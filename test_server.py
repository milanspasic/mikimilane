import io
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import wave
import server

class BackendTests(unittest.TestCase):
    def test_retry_after_accepts_seconds_and_http_dates(self):
        self.assertEqual(server.retry_after_seconds('120', now=0),120)
        self.assertEqual(server.retry_after_seconds('Thu, 01 Jan 1970 00:02:00 GMT', now=0),120)
        self.assertEqual(server.retry_after_seconds(None, now=0),300)
        self.assertEqual(server.retry_after_seconds('invalid', now=0),300)
        self.assertEqual(server.retry_after_seconds('0', now=0),server.REQUEST_PAUSE)

    def test_platform_validation(self):
        self.assertIn('youtube.com', server.validate_url('https://www.youtube.com/watch?v=abcdefghijk'))
        for url in ['http://youtube.com/watch?v=abcdefghijk', 'https://127.0.0.1/a', 'https://example.org/a', 'https://soundcloud.com/artist/sets/list', 'https://youtube.com/playlist?list=x']:
            with self.assertRaises(ValueError): server.validate_url(url)

    def test_timestamp_merging_preserves_later_reappearance(self):
        tracks=[]
        track={'title':'Song','subtitle':'Artist','key':'1','url':'javascript:bad'}
        server.merge_match(tracks,track,24,36)
        server.merge_match(tracks,track,36,48)
        server.merge_match(tracks,track,300,312)
        self.assertEqual(len(tracks),2)
        self.assertEqual(tracks[0]['start'],24)
        self.assertEqual(tracks[0]['end'],48)
        self.assertTrue(tracks[0]['approximate'])
        self.assertIsNone(tracks[0]['link'])

    def test_chunks_have_real_set_relative_offsets(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'audio.wav'
            with wave.open(str(path),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
                f.writeframes(b'\x00\x00'*(16000*27))
            chunks=list(server.audio_chunks(path))
            self.assertEqual([x[0] for x in chunks],[0,12,24])
            self.assertEqual([x[1] for x in chunks],[12,12,3])
            self.assertFalse(any(x[3] for x in chunks))
            with wave.open(io.BytesIO(chunks[0][2])) as f:
                self.assertEqual(f.getnframes(),12*16000)

    def test_one_active_scan(self):
        with patch.object(threading.Thread,'start',lambda *_:None):
            job=server.create_job('https://soundcloud.com/artist/mix',30)
            try:
                with self.assertRaises(BlockingIOError):server.create_job('https://soundcloud.com/artist/mix2',30)
                self.assertNotIn('cancel',server.public_job(job['id']))
            finally:
                server.JOBS.pop(job['id']);server.ACTIVE.release()

    def test_backend_token_and_origin_guards(self):
        with patch.object(server,'ACCESS_TOKEN','x'*40):
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            root=f'http://127.0.0.1:{http.server_port}'
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(root+'/health')
                self.assertEqual(error.exception.code,401)
                auth={'Authorization':'Bearer '+'x'*40}
                with urllib.request.urlopen(urllib.request.Request(root+'/health',headers=auth)) as r:
                    self.assertEqual(json.load(r)['service'],'setlens-free')
                headers={**auth,'Content-Type':'application/json','X-Setlens-Client':'1','Origin':'https://another-site.test'}
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(root+'/v1/scans',headers=headers,data=b'{}'))
                self.assertEqual(error.exception.code,403)
            finally:
                http.shutdown();http.server_close();thread.join()

class RecognitionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_keeps_matches_and_blocks_new_jobs_without_retrying(self):
        from shazamio.exceptions import RateLimited
        calls=[]
        class FakeShazam:
            def __init__(self, **_): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def recognize(self, *_args, **_kwargs):
                calls.append(1)
                if len(calls)==2: raise RateLimited('test response')
                return {'track':{'title':'Known track','subtitle':'Artist','key':'1'}}
        job_id='test-rate-limit'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audio.wav'
            with wave.open(str(path),'wb') as recording:
                recording.setnchannels(1);recording.setsampwidth(2);recording.setframerate(16000)
                recording.writeframes(b'\x01\x00'*(16000*36))
            with patch.object(server,'REQUEST_PAUSE',0),patch.object(server,'NEXT_REQUEST_AT',0),patch.object(server,'COOLDOWN_UNTIL',0),patch('shazamio.Shazam',FakeShazam):
                server.JOBS[job_id]={'cancel':threading.Event(),'tracks':[]}
                try:
                    with self.assertRaises(server.RecognitionFailure) as error:
                        await server.recognize_file(job_id,path)
                    self.assertEqual(error.exception.code,'SHAZAM_RATE_LIMITED')
                    self.assertEqual(error.exception.http_status,429)
                    self.assertEqual(len(calls),2)
                    self.assertEqual(server.JOBS[job_id]['processedSeconds'],12)
                    self.assertEqual(len(server.JOBS[job_id]['tracks']),1)
                    self.assertGreaterEqual(server.recognition_cooldown(),299)
                    with patch.object(threading.Thread,'start') as start:
                        with self.assertRaises(server.RecognitionFailure):server.create_job('https://soundcloud.com/artist/mix',30)
                        start.assert_not_called()
                finally:server.JOBS.pop(job_id,None)

    async def test_request_spacing_applies_across_jobs_and_can_be_cancelled(self):
        with patch.object(server,'REQUEST_PAUSE',.03),patch.object(server,'NEXT_REQUEST_AT',0):
            await server.wait_for_recognition_slot(threading.Event())
            first=time.monotonic()
            await server.wait_for_recognition_slot(threading.Event())
            self.assertGreaterEqual(time.monotonic()-first,.025)
            cancelled=threading.Event();cancelled.set()
            with self.assertRaises(InterruptedError):await server.wait_for_recognition_slot(cancelled)

    async def test_error_messages_separate_rate_limits_timeouts_and_refusals(self):
        from shazamio.exceptions import RateLimited
        rate=server.recognition_failure(RateLimited('private-url-not-to-log'),{'status':429,'retry_after':'180'})
        self.assertEqual(rate.retry_after,180)
        self.assertNotIn('private-url',str(rate))
        self.assertEqual(server.recognition_failure(asyncio.TimeoutError(),{}).code,'SHAZAM_TIMEOUT')
        self.assertEqual(server.recognition_failure(RuntimeError('private detail'),{'status':403}).code,'SHAZAM_ACCESS_REFUSED')

    async def test_json_access_refusal_is_not_treated_as_an_empty_match(self):
        from types import SimpleNamespace
        calls=[]
        class FakeShazam:
            def __init__(self,http_client):self.client=http_client
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def recognize(self,*_,**__):
                calls.append(1)
                params=SimpleNamespace(response=SimpleNamespace(status=403,headers={}))
                for callback in self.client.trace_config.on_request_end:await callback(None,None,params)
                return {'error':'refused'}
        job_id='test-refused'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audio.wav'
            with wave.open(str(path),'wb') as recording:
                recording.setnchannels(1);recording.setsampwidth(2);recording.setframerate(16000)
                recording.writeframes(b'\x01\x00'*(16000*24))
            with patch.object(server,'REQUEST_PAUSE',0),patch.object(server,'NEXT_REQUEST_AT',0),patch('shazamio.Shazam',FakeShazam):
                server.JOBS[job_id]={'cancel':threading.Event(),'tracks':[]}
                try:
                    with self.assertRaises(server.RecognitionFailure) as error:await server.recognize_file(job_id,path)
                    self.assertEqual(error.exception.code,'SHAZAM_ACCESS_REFUSED')
                    self.assertEqual(len(calls),1)
                    self.assertEqual(server.JOBS[job_id]['tracks'],[])
                finally:server.JOBS.pop(job_id,None)

if __name__=='__main__':unittest.main()
