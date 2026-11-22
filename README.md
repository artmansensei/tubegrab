# TubeGrab

A small, self-hosted web app for downloading YouTube videos in the quality you
want — 144p up to 8K — or extracting the audio as MP3.

Paste a link, pick a resolution, download. Everything runs on your own machine;
there is no external service and no tracking.

![TubeGrab](https://img.shields.io/badge/stack-FastAPI%20%2B%20yt--dlp%20%2B%20ffmpeg-ff3d5a)

## Features

- **Every available quality** — the app reads the real format list from YouTube
  and shows each resolution with fps and an estimated file size.
- **Merged MP4 output** — separate video and audio streams are muxed with ffmpeg
  into a single playable file. H.264 is preferred for maximum device support.
- **MP3 extraction** — choose 192, 256 or 320 kbps.
- **Live progress** — speed, ETA and a progress bar, with automatic retries
  using alternate YouTube clients if a download fails.
- **Cancel / clean up** — cancel in-flight downloads and clear finished ones.
- **Local and private** — files stay on disk under `downloads/` and are deleted
  after an hour.
- No database, no accounts, no cloud.

## Requirements

- Python **3.10+**
- **Node.js** (recommended) — modern yt-dlp needs a JavaScript runtime to solve
  YouTube signatures and expose every format. Deno also works.
- ffmpeg is **not** required separately: it ships with the `imageio-ffmpeg` pip
  package.

## Quick start

### Windows

Double-click **`run.bat`** (or run it from a terminal). It creates a virtual
environment, installs dependencies, starts the server and opens your browser.

### macOS / Linux

```bash
./run.sh
```

### Render.com (free, recommended for cloud)

1. Push your code to a GitHub repository.
2. Go to https://dashboard.render.com → **New** → **Web Service**.
3. Connect your GitHub repo. Render auto-detects the `Dockerfile`.
4. Settings:
   - **Name**: `tubegrab`
   - **Plan**: Free
   - **Build command**: (auto from Dockerfile)
   - **Start command**: (auto from Dockerfile)
5. Click **Create Web Service**. Render builds and deploys (~3-5 min).
6. Your app is live at `https://tubegrab.onrender.com`.

> **Free tier notes**: spins down after 15 min of inactivity (first request takes ~30-60s to wake up). Downloads are ephemeral (lost on restart) — use the **Save** button to keep files. Set `DOWNLOAD_DIR=/tmp/downloads` (already in `render.yaml`).

Or use the one-click blueprint: push `render.yaml` to your repo, then **New** → **Blueprint** → select repo.

### Manual (any VPS / server)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:8000>.

Useful options:

```bash
python app.py --port 9000          # different port
python app.py --host 0.0.0.0       # expose on your LAN (see note below)
```

## Usage

1. Paste a YouTube URL (regular video, Shorts, `youtu.be`, or a link with a
   timestamp/playlist parameter).
2. Press **Fetch**.
3. Pick a video quality — or one of the MP3 bitrates.
4. Press **Download**, then **Save** when it is ready.

Files are written to `downloads/` (or the temp folder if that directory is not
writable) and served with the original title as the filename.

## Configuration

| Variable       | Default     | Purpose                          |
| -------------- | ----------- | -------------------------------- |
| `DOWNLOAD_DIR` | `./downloads` | Where finished files are stored |
| `HOST`         | `127.0.0.1` | Bind address                     |
| `PORT`         | `8000`      | Bind port                        |

## API

The frontend is a plain HTML/CSS/JS page on top of a small JSON API.

| Method   | Endpoint                | Description                                  |
| -------- | ----------------------- | -------------------------------------------- |
| `GET`    | `/api/health`           | Version info for yt-dlp, ffmpeg, JS runtime  |
| `POST`   | `/api/info`             | `{ "url": "..." }` → title, thumbnail, qualities |
| `POST`   | `/api/jobs`             | `{ "url", "height"?, "audio_only"?, "audio_bitrate"? }` → job |
| `GET`    | `/api/jobs`             | List jobs (newest first)                     |
| `GET`    | `/api/jobs/{id}`        | Job status and progress                      |
| `GET`    | `/api/jobs/{id}/file`   | Download the finished file                   |
| `DELETE` | `/api/jobs/{id}`        | Cancel and delete a job                      |

Example:

```bash
curl -s -X POST http://127.0.0.1:8000/api/info \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=aqz-KE-bpKQ"}'

curl -s -X POST http://127.0.0.1:8000/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=aqz-KE-bpKQ","height":1080}'
```

## How it works

```
static/index.html ──fetch──▶ FastAPI (app.py) ──▶ yt-dlp ──▶ ffmpeg
        ▲                          │
        └──── poll /api/jobs ◀─────┘  job progress
```

- `POST /api/info` extracts metadata and groups formats by height, preferring
  H.264 and computing an approximate merged size.
- `POST /api/jobs` queues a background download. A format selector such as
  `bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]` is used so the
  result is a well-supported MP4.
- Progress hooks update an in-memory job; the browser polls every ~900 ms.
- Failed attempts retry with the `android_vr` and `tv` YouTube clients, then fall
  back to the best progressive stream.

## Troubleshooting

**"YouTube asked this server to verify it is not a bot."**
YouTube occasionally blocks automated requests from an IP. Wait a minute and try
again, or switch networks. This is a YouTube-side restriction, not an app bug.

**Some resolutions are missing.**
Install Node.js (or Deno) so yt-dlp can solve signatures. Check `/api/health` —
`js_runtime` should not be `null`. Also run `pip install -U yt-dlp`: YouTube
changes frequently and yt-dlp updates often.

**"Failed to extract any player response."**
YouTube changed something. Update yt-dlp: `pip install -U yt-dlp yt-dlp-ejs`. The app already pins recent versions; on Render, trigger a manual redeploy. If it persists, YouTube may be blocking your server's IP — try a different hosting region or wait a few hours.
Try a lower quality, or update yt-dlp. The app already downloads fragments in
parallel and retries failed fragments.

**Nothing plays after downloading.**
The file is a standard MP4 (H.264/AAC when available). Very high resolutions may
use VP9/AV1, which some older players cannot decode — try 1080p or lower.

**Port already in use.**
Run `python app.py --port 8001`.

## Notes

- Playlist links are rejected on purpose; paste a single video instead.
- Live streams are not supported.
- If you bind to `0.0.0.0`, anyone on your network can use the app. There is no
  authentication, so only do that on a trusted network.

## Legal

Download only content you own or have permission to download. Respect YouTube's
Terms of Service and the rights of copyright holders.
