# Setlens Free backend

This is a real HTTP API and local web interface for finding songs in a DJ set with timestamps. It uses the free, unofficial ShazamIO client. It does not create its own music catalogue and does not provide guaranteed, unlimited access to Shazam. No AudD or RapidAPI account/key is needed.

The hosted Setlens website is prepared to connect to ONE copy of this backend. The owner deploys it once and adds its HTTPS address and private access token under Owner setup. Visitors sign in with ChatGPT and paste a set link. They never enter or see the backend token. That token protects YOUR server; it is not a purchased music-recognition key.

## Run on your Mac, free

1. Install Python 3.11 or newer from https://www.python.org/downloads/ if `python3 --version` is unavailable or too old.
2. For YouTube links, install Node.js 22+ (LTS) from https://nodejs.org/en/download/. The package's yt-dlp JavaScript helpers are included automatically. Deno 2.3+ also works.
3. Unzip this folder into Downloads. Open Terminal and run:

```sh
cd ~/Downloads/setlens-free
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

4. Open http://127.0.0.1:8765 in your browser. Paste a public YouTube or SoundCloud recording link and click Find tracks. Keep Terminal open.
5. Stop the server with Ctrl+C. To run again:

```sh
cd ~/Downloads/setlens-free
source .venv/bin/activate
python server.py
```

The localhost address is only for YOUR computer. Putting that address into the hosted website will not connect it. A shared website needs an online backend with HTTPS.

You can also scan your own audio file directly without downloading a platform recording:

```sh
python server.py --audio /full/path/to/your-mix.mp3 --minutes 120
```

This writes a JSON tracklist beside that audio file. That mode is local and does not start a web server.

## Host once for website visitors

The Dockerfile and render.yaml are ready for a single hobby web service. A host still has its own resource, bandwidth, sleep and fair-use limits. Free recognition does not guarantee free hosting at arbitrary scale.

For Render's free hobby tier:

1. Put the files in this folder in a GitHub repository.
2. Connect that repository to a new Render Web Service using Docker. Select the Free instance type explicitly.
3. Set `SETLENS_ACCESS_TOKEN` to a newly generated private random token with at least 32 characters. Generate one in Terminal with:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

4. The Dockerfile sets `SETLENS_HOST=0.0.0.0`; Render supplies PORT. The server refuses non-local listening unless the private token is configured.
5. Deploy and copy the server's public HTTPS address.
6. Sign in to the existing Setlens site as its owner. Open Settings, select ShazamIO backend, enter the HTTPS address and token, and Save shared connection. The website tests /health before saving and encrypts the token. Set a shared daily processing budget.
7. Visitors use the site without API keys. Each has their own saved sets and tracks. On a free host, keep the results page open during a scan: the site's polling also prevents idle shutdown while it is being used. Results on this simple backend are in memory and are lost on a restart. Their completed copies are saved to the website when it next polls the backend. Do not rely on unattended jobs on an idle/sleeping free service.

Render free-tier docs: https://render.com/docs/free
Docker deployment docs: https://render.com/docs/docker

## API

Send `Authorization: Bearer <SETLENS_ACCESS_TOKEN>` on all online requests. This credential belongs only on your website's server. The local 127.0.0.1 mode can work without it. No permissive CORS is enabled; browsers should use the hosted website's own protected API routes.

- `GET /health` checks service identity, version and JavaScript runtime availability.
- `POST /v1/scans` with JSON `{"url":"https://soundcloud.com/artist/mix","scanMinutes":120}` starts an asynchronous job and returns its id.
- `GET /v1/scans/{id}` returns status, stage, tracks and actual checked duration.
- `POST /v1/scans/{id}/cancel` stops a job while keeping partial results.
- Every POST also needs `Content-Type: application/json` and `X-Setlens-Client: 1`.

One set runs at a time. Recognition uses consecutive 12-second audio windows, with a three-second pause between network recognition requests and no automatic retries. Approximate timestamps use the beginning of the recognised sample in the set, not Shazam's position inside the original song. Nearby repeated matches merge; later reappearances remain separate. No matches are invented. A duration cap can truncate longer sets and is disclosed.

Temporary downloaded/converted audio is deleted when a scan finishes. Platform restrictions are respected: no login cookies or attempts to bypass private, paid or restricted recordings are included. If a public link cannot download, try another one or your own local audio file. ShazamIO and yt-dlp are third-party dependencies and may need maintenance as upstream services change.

## Verification and limitations

Run `python -m unittest -v test_server.py`. Tests cover URL validation, real sample offsets, duplicate matches, concurrency, secret-token checks and cross-origin protection.

During authoring, the live pipeline correctly identified the maintainer's public Gloria.ogg sample as Gloria Gaynor — I Will Survive, starting at 00:00, and merged repeated detections. A later upstream request failed and the backend correctly kept partial results. A complete long DJ-set scan has not been verified. Treat this free engine as experimental; availability and recognition accuracy are not guaranteed.

Source for the recognition client: https://github.com/shazamio/ShazamIO
