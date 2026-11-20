"""Official AudioTag API adapter. Only the free allowance is used; no paid fallback."""
from __future__ import annotations
import io
import json
import math
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

API_URL = 'https://audiotag.info/api'
CHUNK_SECONDS = 180

class AudioTagFailure(RuntimeError):
    def __init__(self, message, code, http_status=None):
        super().__init__(message)
        self.code, self.http_status, self.retry_after = code, http_status, 0

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None

def api_call(key, action, fields=None, audio=None):
    # Never include the key in a URL, exception, log or redirect request.
    values = {'apikey': key, 'action': action, **(fields or {})}
    if audio is None:
        body = urllib.parse.urlencode(values).encode()
        content_type = 'application/x-www-form-urlencoded'
    else:
        boundary = 'setlens-' + secrets.token_hex(16)
        sections = []
        for name, value in values.items():
            sections.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        sections.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="fragment.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(), audio, f'\r\n--{boundary}--\r\n'.encode()])
        body = b''.join(sections)
        content_type = 'multipart/form-data; boundary=' + boundary
    request = urllib.request.Request(API_URL, data=body, headers={'Content-Type': content_type, 'User-Agent': 'Setlens/3'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            data = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        raise AudioTagFailure(f'AudioTag returned HTTP {error.code}. This scan stopped; partial results are kept.', 'AUDIOTAG_HTTP_ERROR', error.code) from None
    except Exception:
        raise AudioTagFailure('AudioTag could not be reached. No recognition upload was automatically repeated; partial results are kept.', 'AUDIOTAG_CONNECTION_FAILED') from None
    if not isinstance(data, dict):
        raise AudioTagFailure('AudioTag returned an invalid response.', 'AUDIOTAG_INVALID_RESPONSE')
    if data.get('success') is not True:
        error = str(data.get('error', '')).lower()
        if 'api key' in error:
            raise AudioTagFailure('The owner needs to enter an active AudioTag API key in Settings.', 'AUDIOTAG_KEY_INVALID')
        if 'credit' in error or 'balance' in error:
            raise AudioTagFailure('The shared AudioTag free allowance is exhausted. This scan stopped; partial results are kept. No paid fallback was used.', 'AUDIOTAG_FREE_QUOTA')
        raise AudioTagFailure('AudioTag could not process this request. This scan stopped; partial results are kept.', 'AUDIOTAG_RESPONSE_FAILED')
    return data

def free_seconds(key):
    stats = api_call(key, 'stat')
    value = stats.get('identification_free_sec_remainder')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise AudioTagFailure('AudioTag did not confirm a free allowance. No audio was uploaded.', 'AUDIOTAG_FREE_QUOTA_UNKNOWN')
    # A funded account might spend credits if its free seconds change elsewhere.
    # Refuse it entirely in this free-only integration.
    balance = stats.get('current_credit_balance')
    if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance) or balance != 0:
        raise AudioTagFailure('Use an AudioTag account with a zero paid-credit balance for free-only recognition. No audio was uploaded.', 'AUDIOTAG_FREE_ONLY')
    return math.floor(value)

def audio_chunks(path):
    with wave.open(str(path), 'rb') as recording:
        if recording.getnchannels() != 1 or recording.getsampwidth() != 2 or recording.getframerate() != 8000:
            raise ValueError('AudioTag needs mono 16-bit 8 kHz audio.')
        offset = 0
        while True:
            raw = recording.readframes(8000 * CHUNK_SECONDS)
            if not raw:
                break
            duration = len(raw) / 16000
            if duration < 5:
                break
            output = io.BytesIO()
            with wave.open(output, 'wb') as fragment:
                fragment.setnchannels(1); fragment.setsampwidth(2); fragment.setframerate(8000)
                fragment.writeframes(raw)
            yield offset, duration, output.getvalue()
            offset += duration

def recognize_chunk(key, audio, duration, cancelled):
    if cancelled.is_set():
        raise InterruptedError('Scan stopped. Partial results are kept.')
    if free_seconds(key) < math.ceil(duration):
        raise AudioTagFailure('The shared AudioTag free allowance is too small for the next audio fragment. This scan stopped; partial results are kept. No paid fallback was used.', 'AUDIOTAG_FREE_QUOTA')
    submitted = api_call(key, 'identify', {'start_time': 0, 'time_len': math.ceil(duration)}, audio)
    token = submitted.get('token')
    if not isinstance(token, str) or not token:
        raise AudioTagFailure('AudioTag did not return a recognition job. No upload was repeated.', 'AUDIOTAG_INVALID_RESPONSE')
    # Only poll the existing query token. Never repeat the identify POST.
    for _ in range(100):
        if cancelled.wait(2):
            raise InterruptedError('Scan stopped. Partial results are kept.')
        result = api_call(key, 'get_result', {'token': token})
        status = result.get('result')
        if status == 'found':
            if not isinstance(result.get('data'), list):
                raise AudioTagFailure('AudioTag returned invalid track data.', 'AUDIOTAG_INVALID_RESPONSE')
            return result['data']
        if status == 'not found':
            return []
        if status != 'wait':
            raise AudioTagFailure('AudioTag could not return this recognition result. No upload was repeated.', 'AUDIOTAG_RESULT_UNAVAILABLE')
    raise AudioTagFailure('AudioTag did not finish this fragment in time. No upload was repeated; partial results are kept.', 'AUDIOTAG_TIMEOUT')

def matches(data, offset, duration):
    """Candidates within a result interval are alternatives, not separate songs."""
    candidates = []
    for group in data:
        if not isinstance(group, dict) or not isinstance(group.get('tracks'), list):
            continue
        interval = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*', str(group.get('time', '')))
        if not interval:
            continue
        start, end = map(float, interval.groups())
        if start < 0 or end <= start or start >= duration:
            continue
        for track in group['tracks']:
            if isinstance(track, list) and len(track) >= 2 and all(isinstance(v, str) and v.strip() for v in track[:2]):
                # The first record is the provider's primary candidate for this interval.
                candidates.append((offset + start, offset + min(end, duration), {'title': track[0], 'subtitle': track[1]}))
                break
    return sorted(candidates, key=lambda item: item[0])
