# ⚡ Grok Studio

A small web wrapper around the local **Grok CLI**. Type a natural-language
request in the browser; Grok runs headless on the host, does the work (image
generation/editing, image→video, video generation, video extend, frame
extraction, general agent tasks), and the results stream into a live gallery.

```
browser ──HTTPS──> Cloudflare ──Tunnel──> FastAPI (:8799)
                                          ├─ spawns `grok -p … --output-format streaming-json`
                                          ├─ media pipeline CLIs (bin/)
                                          └─ filesystem watcher → live media gallery
```

## What it does
- **One chat box, many media tasks** — text→image, image→video (keeps the real
  image, no text round-trip), text→video, video extend / 이어붙이기, video→frame.
- **Live agent log** — streams Grok's thoughts + answer as it works.
- **Auto gallery** — any image/video written into the workspace shows up
  automatically; click to preview (with scrubbing/mute) or download.
- **Upload** — drop a file in and its path is added to your prompt so the agent
  can act on it.
- **Settings panel** — model / effort / max-turns / best-of-n / web-search /
  persistent rules, saved server-side.
- **Password gate** + recommended Cloudflare Access in front.

## Layout
```
app/main.py            FastAPI backend (grok runner, gallery, upload, auth, settings, watcher)
app/static/index.html  single-file SPA (no build step)
bin/img2video          image → video via the xAI video API (real first frame)
bin/videoextend        make a video longer (last-frame → image-to-video → join; or native xAI extend)
bin/vidframe           pull a still frame from a video (--at last|first|middle|<seconds>)
requirements.txt       Python deps
```
> `bin/videoextend` runs on the project venv (its shebang points at `.venv`)
> because it needs OpenCV; `bin/img2video` and `bin/vidframe` are stdlib-only.

## Requirements
- Linux (developed on WSL2 / Ubuntu), Python 3.10+.
- An authenticated **Grok CLI** on PATH (its OAuth token in `~/.grok/auth.json`
  is reused for the xAI video API — no separate key needed).
- `ffmpeg` — easiest via the `imageio-ffmpeg` wheel (no system install):
  ```bash
  .venv/bin/pip install imageio-ffmpeg
  ln -s "$(.venv/bin/python -c 'import imageio_ffmpeg,sys; sys.stdout.write(imageio_ffmpeg.get_ffmpeg_exe())')" bin/ffmpeg
  ```

## Run locally
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
GROK_WEB_WORKSPACE=/path/to/your/media .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8799
```
First run generates an access password (printed in the console, saved to
`app/.password`) and a cookie-signing secret (`app/.secret`). Open
`http://127.0.0.1:8799`.

### Configuration (environment variables)
| var | default | purpose |
|-----|---------|---------|
| `GROK_WEB_WORKSPACE` | `./workspace` | folder Grok reads/writes; the gallery watches it |
| `GROK_WEB_GROK_BIN` | `grok` | path to the grok CLI |
| `GROK_WEB_HOST` / `GROK_WEB_PORT` | `127.0.0.1` / `8799` | bind address |
| `GROK_WEB_REQUIRE_PASSWORD` | `1` | set `0` when a gateway (e.g. Cloudflare Access) already authenticates |
| `GROK_WEB_POLL_WATCH` | `0` | `1` = poll the workspace instead of inotify |
| `GROK_WEB_INACTIVITY_TIMEOUT` | `600` | seconds with no Grok output before a run is abandoned |

## Deploy (always-on)
Runs well as a **systemd user service** behind a dedicated Cloudflare tunnel:
```ini
# ~/.config/systemd/user/grok-studio.service
[Service]
Environment=GROK_WEB_WORKSPACE=%h/grok-media
Environment=GROK_WEB_REQUIRE_PASSWORD=0
ExecStart=%h/grok-studio/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8799
WorkingDirectory=%h/grok-studio
```
```bash
systemctl --user enable --now grok-studio
loginctl enable-linger "$USER"   # keep it running after logout
```

### Lock it down (important)
The agent can run shell commands on the host — **do not leave it open**:
1. **Cloudflare Access** (recommended): Zero Trust → Access → Applications → add
   your hostname, policy = allow your account only. Then set
   `GROK_WEB_REQUIRE_PASSWORD=0`.
2. The built-in **password gate** stays as a second layer.

## Notes
- Generated media, the cookie secret, the access password, cached thumbnails and
  personal settings are intentionally **git-ignored** — keep them off the repo.
- The native xAI video-extend path applies strict content moderation; the
  default `videoextend` method is local concat, which is moderation-agnostic.
