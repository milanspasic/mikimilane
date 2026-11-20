import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave
import audiotag
import server

class AudioTagTests(unittest.TestCase):
    def test_only_confirmed_free_unfunded_accounts_can_upload(self):
        for stats in ({'current_credit_balance': 0}, {'identification_free_sec_remainder': 100, 'current_credit_balance': 1}, {'identification_free_sec_remainder': '100', 'current_credit_balance': 0}, {'identification_free_sec_remainder': 100}, {'identification_free_sec_remainder': float('nan'), 'current_credit_balance': 0}):
            with patch.object(audiotag, 'api_call', return_value=stats):
                with self.assertRaises(audiotag.AudioTagFailure): audiotag.free_seconds('private')
        with patch.object(audiotag, 'api_call', return_value={'identification_free_sec_remainder': 100, 'current_credit_balance': 0}):
            self.assertEqual(audiotag.free_seconds('private'), 100)

    def test_exhausted_allowance_stops_before_any_identify(self):
        with patch.object(audiotag, 'free_seconds', return_value=29), patch.object(audiotag, 'api_call') as call:
            with self.assertRaises(audiotag.AudioTagFailure) as error:
                audiotag.recognize_chunk('private', b'audio', 30, threading.Event())
            self.assertEqual(error.exception.code, 'AUDIOTAG_FREE_QUOTA')
            call.assert_not_called()

    def test_existing_query_is_polled_without_reupload(self):
        responses = [{'token': 'query'}, {'result': 'wait'}, {'result': 'found', 'data': [{'time': '5 - 20', 'tracks': [['Song', 'Artist']]}]}]
        cancelled = threading.Event()
        with patch.object(audiotag, 'free_seconds', return_value=100), patch.object(audiotag, 'api_call', side_effect=responses) as call, patch.object(cancelled, 'wait', return_value=False):
            result = audiotag.recognize_chunk('private', b'audio', 30, cancelled)
            self.assertEqual([c.args[1] for c in call.call_args_list], ['identify', 'get_result', 'get_result'])
            self.assertEqual(audiotag.matches(result, 180, 30)[0][:2], (185, 200))

    def test_ambiguous_upload_failure_is_not_retried(self):
        with patch.object(audiotag, 'free_seconds', return_value=100), patch.object(audiotag, 'api_call', side_effect=audiotag.AudioTagFailure('Failed', 'AUDIOTAG_CONNECTION_FAILED')) as call:
            with self.assertRaises(audiotag.AudioTagFailure): audiotag.recognize_chunk('private', b'audio', 30, threading.Event())
            self.assertEqual(call.call_count, 1)

    def test_candidates_are_alternatives_and_intervals_are_set_relative(self):
        data = [{'time': '0 - 20', 'tracks': [['Song', 'Artist'], ['Another release', 'Various']]}, {'time': '25 - 45', 'tracks': [['Next', 'DJ']]}, {'time': 'bad', 'tracks': [['Fake', 'Bad']]}, {'time': '90 - 100', 'tracks': [['Outside', 'Bad']]}]
        matches = audiotag.matches(data, 180, 30)
        self.assertEqual([(a,b,t['title']) for a,b,t in matches], [(180,200,'Song'), (205,210,'Next')])

    def test_partial_matches_survive_free_quota_exhaustion(self):
        job = 'audiotag-partial'
        server.JOBS[job] = {'cancel': threading.Event()}
        def identify(_key, _data, _seconds, _cancelled):
            if not server.JOBS[job].get('tracks'): return [{'time': '10 - 20', 'tracks': [['Song', 'Artist']]}]
            raise audiotag.AudioTagFailure('Free quota exhausted', 'AUDIOTAG_FREE_QUOTA')
        try:
            with patch.object(audiotag, 'audio_chunks', return_value=iter([(0,180,b'a'),(180,180,b'b')])), patch.object(audiotag, 'recognize_chunk', side_effect=identify):
                with self.assertRaises(audiotag.AudioTagFailure): server.recognize_audiotag(job, Path('unused'), 'private')
                self.assertEqual(server.JOBS[job]['processedSeconds'], 180)
                self.assertEqual(server.JOBS[job]['tracks'][0]['start'], 10)
        finally: server.JOBS.pop(job)

    def test_audiotag_jobs_are_separate_from_shazam_cooldown_and_hide_key(self):
        with patch.object(server, 'COOLDOWN_UNTIL', 99999999999), patch.object(threading.Thread, 'start'):
            job = server.create_job('https://soundcloud.com/artist/mix', 30, engine='audiotag', api_key='test-private')
            try:
                self.assertNotIn('test-private', json.dumps(job))
                self.assertNotIn('apiKey', job)
            finally: server.JOBS.pop(job['id']); server.ACTIVE.release()

    def test_audio_chunks_cover_the_recording_and_keep_real_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'recording.wav'
            with wave.open(str(path), 'wb') as recording:
                recording.setnchannels(1); recording.setsampwidth(2); recording.setframerate(8000)
                recording.writeframes(b'\x01\x00' * 8000 * 190)
            chunks = list(audiotag.audio_chunks(path))
            self.assertEqual([(c[0],c[1]) for c in chunks], [(0,180),(180,10)])
            with wave.open(io.BytesIO(chunks[1][2]), 'rb') as recording: self.assertEqual(recording.getnframes(),80000)

    def test_multipart_endpoint_does_not_leak_key_or_follow_redirects(self):
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,*_):return b'{"success":true,"token":"job"}'
        with patch.object(audiotag.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = Reply()
            audiotag.api_call('test-private', 'identify', {'start_time':0, 'time_len':30}, b'audio')
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'https://audiotag.info/api')
            self.assertIn(b'name="apikey"\r\n\r\ntest-private',request.data)
            self.assertIn(b'filename="fragment.wav"',request.data)
            self.assertIsInstance(build.call_args.args[0],audiotag.NoRedirect)
            self.assertIsNone(audiotag.NoRedirect().redirect_request(None,None,None,None,None,None))

if __name__ == '__main__': unittest.main()
