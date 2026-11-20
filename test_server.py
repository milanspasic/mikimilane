import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import wave
import server

class BackendTests(unittest.TestCase):
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

if __name__=='__main__':unittest.main()
