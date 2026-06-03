"""
Grok Studio — web wrapper around the local Grok CLI.

You chat like in the console; Grok works in whatever folder you pick (its
--cwd), grabs files there, edits them, and drops results back as files.
A filesystem watcher surfaces any new image/video in that folder live.

Pure stdlib + FastAPI. No Node toolchain required.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import signal
import string
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse, JSONResponse, FileResponse, RedirectResponse,
    StreamingResponse, PlainTextResponse,
)
from itsdangerous import TimestampSigner, BadSignature
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
try:
    from watchdog.observers.polling import PollingObserver
except Exception:
    PollingObserver = Observer

try:
    import cv2  # server-side video thumbnail extraction
except Exception:
    cv2 = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def _env(key: str, default: str) -> str:
    val = os.environ.get(key)
    return val if val not in (None, "") else default


GROK_BIN = _env("GROK_WEB_GROK_BIN", r"C:\Users\necro\.grok\bin\grok.exe")
DEFAULT_FOLDER = Path(_env("GROK_WEB_WORKSPACE", r"C:\ai")).resolve()
MODEL = _env("GROK_WEB_MODEL", "grok-build")
HOST = _env("GROK_WEB_HOST", "127.0.0.1")
PORT = int(_env("GROK_WEB_PORT", "8799"))
MAX_TURNS = _env("GROK_WEB_MAX_TURNS", "")

# Grok stores generated media under ~/.grok/sessions/<cwd>/<session-id>/{images,videos}/
GROK_HOME = Path(GROK_BIN).resolve().parent.parent
SESSIONS_DIR = GROK_HOME / "sessions"

# cached video thumbnails (jpg)
THUMBS_DIR = APP_DIR / ".thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# server-side execution options (shared across devices/browsers)
SETTINGS_FILE = APP_DIR / "settings.json"
_ALLOWED_OPT_KEYS = {"model", "effort", "maxTurns", "bestOfN", "check", "webSearch", "rules", "customFlags"}


def _read_settings_file() -> dict:
    try:
        d = json.loads(SETTINGS_FILE.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_settings_file(d: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass

# ---- auth ----------------------------------------------------------------
COOKIE = "grok_auth"
MAX_AGE = 7 * 24 * 3600
_secret_file = APP_DIR / ".secret"
if _secret_file.exists():
    SECRET = _secret_file.read_text(encoding="utf-8").strip()
else:
    SECRET = secrets.token_hex(32)
    _secret_file.write_text(SECRET, encoding="utf-8")
signer = TimestampSigner(SECRET)

PASSWORD = _env("GROK_WEB_PASSWORD", "")
_pw_file = APP_DIR / ".password"
if not PASSWORD:
    if _pw_file.exists():
        PASSWORD = _pw_file.read_text(encoding="utf-8").strip()
    else:
        PASSWORD = secrets.token_urlsafe(9)
        _pw_file.write_text(PASSWORD, encoding="utf-8")

# When false, the app's own login is bypassed (front it with Cloudflare Access).
REQUIRE_PASSWORD = _env("GROK_WEB_REQUIRE_PASSWORD", "1").lower() not in ("0", "false", "no", "off")

# ---- media ---------------------------------------------------------------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
EXCLUDE_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".grok", ".idea", ".vscode",
                "_work", ".tmp", "tmp", "temp", ".cache", ".thumbs", "_frametest"}

# Appended to every grok run: keep intermediate artifacts out of the working folder.
INTERMEDIATE_RULE = (
    "Working-directory hygiene: temporary/intermediate files that are NOT the final deliverable "
    "(individual video frames extracted only to rebuild a video, scratch images, temp clips) must be "
    "created in a dedicated temporary directory OUTSIDE the working directory — prefer a fast local "
    "temp such as /tmp/grok_work on Linux — and deleted when done. The working directory may be a slow "
    "mounted drive, so never write large numbers of intermediate frame images there. Only final "
    "deliverables remain in the working directory. "
    "Deliver each final result as EXACTLY ONE file: do not save the same output to more than one path, "
    "and do not also copy it into a subdirectory (e.g. generated_videos/, generated/, outputs/). "
    "One result = one file at one location. Never leave duplicate copies of the same deliverable."
)

# image-to-video helper: grok's built-in video_gen is TEXT-ONLY, so image->video via video_gen
# means describing the picture in words and losing it. This CLI sends the real image to xAI's
# grok-imagine-video model (image preserved as the first frame).
IMG2VIDEO_BIN = APP_DIR.parent / "bin" / "img2video"
VIDFRAME_BIN = APP_DIR.parent / "bin" / "vidframe"
VIDEOEXTEND_BIN = APP_DIR.parent / "bin" / "videoextend"
MEDIA_ROUTING_RULE = (
    "MEDIA TASK ROUTING — choose EXACTLY ONE path from what the user gives you and what they want, and use it "
    "directly (no exploring/guessing). ffmpeg is NOT on PATH and system python has no opencv, so for video "
    "frame/extend work you MUST use the CLIs below — ad-hoc ffmpeg/opencv WILL fail. Each prints 'OK: <path>'.\n"
    "• text -> image: use the built-in image_gen tool.\n"
    "• text -> video (NO source image): use the built-in video_gen tool directly. Do NOT first make an image "
    "and animate it unless the user explicitly asks for maximum quality.\n"
    "• an IMAGE -> video (animate/bring a picture to life): video_gen is TEXT-ONLY and would describe-then-"
    "destroy the image, so you MUST run:\n"
    f"    {IMG2VIDEO_BIN} \"<image>\" \"<motion prompt>\" [--duration N] [--resolution 720p] [--aspect 9:16]\n"
    "  NEVER use video_gen when a source image exists.\n"
    "• a VIDEO -> make it LONGER / extend / continue / 이어붙이기: run this ONE command and nothing else — it "
    "ALREADY handles frame extraction + continuation + joining:\n"
    f"    {VIDEOEXTEND_BIN} \"<video>\" \"<motion / scene prompt>\" [--method concat|native] [--seconds N] [--resolution 720p] [--loops K]\n"
    "  Default --method concat (last-frame -> image-to-video -> join) works with ALL content including adult. "
    "Use --method native ONLY if the user explicitly wants the smoother xAI-native extension — warn that native "
    "applies strict content moderation and will reject adult content. Do NOT do this by hand — just call videoextend once.\n"
    "• a VIDEO -> pull a still IMAGE / single frame: run:\n"
    f"    {VIDFRAME_BIN} \"<video>\" --at <when>\n"
    "  where <when> is the time the user asked for: a number of seconds (e.g. --at 3 = the frame about 3 seconds "
    "in, --at 7.5 = 7.5s), or last / first / middle. Honor the requested moment exactly; default to last only if "
    "the user gave no time.\n"
    "• RUNNING THESE CLIs: they stream progress and finish with one line — 'OK: <path>' on success or "
    "'ERROR: ...' on failure. A generation takes ~30-90s, so give the command a GENEROUS timeout (~600000 ms) and "
    "just wait — never kill it early, and prefer foreground over background-polling. If a CLI prints 'ERROR: "
    "BLOCKED' or anything about content moderation / 'rejected', xAI refused THAT request: STOP, tell the user it "
    "was blocked and to try a less explicit prompt / different seed frame, and do NOT retry the same request — "
    "every retry re-charges credits and will be blocked again. Only retry on a genuine transient error "
    "(timeout/network).\n"
    "image_gen and image_edit are otherwise unchanged."
)

# the user is waiting in a web UI — finish as soon as the deliverable exists, no QA tail
FAST_FINISH_RULE = (
    "SPEED & STOP: you run in a web app where the user is actively watching and waiting. The moment the "
    "requested deliverable file(s) exist on disk, you are DONE — reply with ONE short confirmation line and "
    "stop. Do NOT run any verification / self-review / 'check-work' / QA pass; do NOT spawn a verifier, "
    "reviewer, or checker subagent; do NOT regenerate 'to be safe'; do NOT re-read, re-open, or re-validate "
    "the output; do NOT write summary / checklist / report files. Producing the result IS the whole job, and "
    "extra checking just makes the user wait. Trust the tool output and end the turn immediately."
)

for _e, _ct in {".webp": "image/webp", ".avif": "image/avif", ".mkv": "video/x-matroska",
                ".m4v": "video/mp4", ".mov": "video/quicktime"}.items():
    mimetypes.add_type(_ct, _e)

DEFAULT_FOLDER.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
app = FastAPI(title="Grok Studio")
state = {"folder": DEFAULT_FOLDER}          # current working folder (the grok --cwd)
clients: set[WebSocket] = set()             # all sockets (for media_changed)
jobs: dict[str, dict] = {}                  # job_id -> job (survives WS disconnect)
sid_job: dict[str, str] = {}                # browser sid -> latest job_id
grok_sessions: dict[str, str] = {}
_harvested: set[str] = set()                # source paths already copied into a workspace
_media_dirty: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _harvest_session_media(session_id: str, dest: Path) -> list[Path]:
    """Copy media Grok generated in its session folder into the working folder
    so it shows up in the gallery. Skips files already present (same size) or
    already harvested."""
    if not session_id or not SESSIONS_DIR.exists():
        return []
    copied: list[Path] = []
    for sub in ("images", "videos"):
        for sdir in SESSIONS_DIR.glob(f"*/{session_id}/{sub}"):
            try:
                entries = sorted(sdir.iterdir())
            except OSError:
                continue
            for f in entries:
                try:
                    if not f.is_file() or f.suffix.lower() not in MEDIA_EXTS:
                        continue
                    st = f.stat()
                    key = f"{f}|{st.st_mtime_ns}|{st.st_size}"
                    if key in _harvested:
                        continue
                    # already delivered to the folder (e.g. grok also wrote it there)?
                    if any(p.is_file() and p.stat().st_size == st.st_size
                           for p in dest.glob(f"*{f.suffix}")):
                        _harvested.add(key)
                        continue
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    target = dest / f"grok_{stamp}_{session_id[:6]}_{f.stem}{f.suffix}"
                    n = 1
                    while target.exists():
                        target = dest / f"grok_{stamp}_{session_id[:6]}_{f.stem}_{n}{f.suffix}"
                        n += 1
                    shutil.copy2(f, target)
                    _harvested.add(key)
                    copied.append(target)
                except OSError:
                    continue
    return copied


def cur() -> Path:
    return state["folder"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _is_authed(token: str | None) -> bool:
    if not REQUIRE_PASSWORD:
        return True
    if not token:
        return False
    try:
        signer.unsign(token, max_age=MAX_AGE)
        return True
    except Exception:
        return False


LOGIN_HTML = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Grok Studio · 로그인</title><style>
*{box-sizing:border-box}body{margin:0;height:100vh;display:grid;place-items:center;
background:#0a0a0f;color:#e8e8f0;font-family:system-ui,'Segoe UI',sans-serif}
.card{width:340px;padding:36px 32px;background:#13131b;border:1px solid #26263a;
border-radius:18px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
h1{margin:0 0 4px;font-size:22px}.sub{color:#7a7a92;font-size:13px;margin-bottom:24px}
input{width:100%;padding:13px 14px;margin-bottom:14px;background:#0c0c12;
border:1px solid #2c2c42;border-radius:10px;color:#fff;font-size:15px}
input:focus{outline:none;border-color:#6d5efc}
button{width:100%;padding:13px;background:linear-gradient(135deg,#6d5efc,#a44bff);
border:0;border-radius:10px;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.err{color:#ff6b6b;font-size:13px;min-height:18px;margin-top:6px}
</style></head><body><form class=card method=post action=/login>
<h1>⚡ Grok Studio</h1><div class=sub>이미지 · 영상 · 에이전트</div>
<input type=password name=password placeholder="접속 비밀번호" autofocus>
<button>들어가기</button><div class=err>__ERR__</div>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", ""))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    if secrets.compare_digest(str(form.get("password", "")), PASSWORD):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, signer.sign(b"ok").decode(), max_age=MAX_AGE,
                        httponly=True, samesite="lax")
        return resp
    return HTMLResponse(LOGIN_HTML.replace("__ERR__", "비밀번호가 틀렸습니다."), status_code=401)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"


OPEN_PATHS = {"/login", "/health", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in OPEN_PATHS:
        return await call_next(request)
    if not _is_authed(request.cookies.get(COOKIE)):
        if path == "/":
            return RedirectResponse("/login")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# App + folder/media APIs
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
async def api_config():
    return {"folder": str(cur()), "model": MODEL,
            "image_exts": sorted(IMAGE_EXTS), "video_exts": sorted(VIDEO_EXTS)}


@app.get("/api/settings")
async def get_settings():
    """Execution options, stored server-side so every device sees the same config."""
    return _read_settings_file()


@app.post("/api/settings")
async def post_settings(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"error": "invalid body"}, status_code=400)
    clean = {k: v for k, v in data.items() if k in _ALLOWED_OPT_KEYS}
    _write_settings_file(clean)
    return {"ok": True, "settings": clean}


@app.get("/api/models")
async def api_models():
    out = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            GROK_BIN, "models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        data, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        out = data.decode("utf-8", "replace")
    except Exception:
        out = ""
    models, default, in_list = [], MODEL, False
    for line in out.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("default model:"):
            parts = s.split(":", 1)[1].strip().split()
            if parts:
                default = parts[0]
        elif low.startswith("available models"):
            in_list = True
        elif in_list and s:
            mm = re.match(r"^\*?\s*([A-Za-z0-9][\w.\-]*)", s)
            if mm:
                models.append(mm.group(1))
    seen, uniq = set(), []
    for m in (models or [MODEL]):
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return {"models": uniq, "default": default}


def _read_signals(session_id: str) -> dict | None:
    """Read grok's per-session signals.json (context/token usage). Glob by id so the
    cwd-encoding of the parent dir doesn't matter."""
    if not session_id or not SESSIONS_DIR.exists():
        return None
    for sig in SESSIONS_DIR.glob(f"*/{session_id}/signals.json"):
        try:
            return json.loads(sig.read_text("utf-8"))
        except Exception:
            return None
    return None


@app.get("/api/usage")
async def api_usage(sid: str = "default"):
    """Current conversation's context-window usage (grok's top-bar `N / 512K`
    indicator). For plan/credit usage (`/usage show`), see /api/credits."""
    sess = grok_sessions.get(sid) or ""
    out = {"sessionId": sess, "available": False, "contextTokensUsed": 0,
           "contextWindowTokens": 0, "pct": 0.0, "turnCount": 0,
           "toolCallCount": 0, "model": MODEL}
    sig = _read_signals(sess)
    if sig:
        used = int(sig.get("contextTokensUsed") or 0)
        win = int(sig.get("contextWindowTokens") or 0)
        out.update({
            "available": True,
            "contextTokensUsed": used,
            "contextWindowTokens": win,
            "pct": round(used / win * 100, 1) if win else 0.0,
            "turnCount": int(sig.get("turnCount") or 0),
            "toolCallCount": int(sig.get("toolCallCount") or 0),
            "model": sig.get("primaryModelId") or MODEL,
        })
    return out


# ── plan/credit usage (grok TUI `/usage show`) ───────────────────────────────
# grok fetches credit usage over its gateway (no local file, no REST endpoint we
# can call directly), so we headlessly drive the TUI, type `/usage show`, and
# scrape the rendered panel via a VT100 emulator. Result is cached.
_credits_cache: dict = {"data": None, "ts": 0.0, "loading": False}
CREDITS_TTL = 90.0
# scrape runs in this throwaway cwd so its ephemeral grok sessions land under a
# separate encoded-cwd path we can safely wipe — never touching real workspace sessions
_PROBE_DIR = Path(tempfile.gettempdir()) / "grok-credits-probe"


def _wipe_probe_sessions():
    try:
        for p in SESSIONS_DIR.glob("*grok-credits-probe*"):
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def _scrape_credits(timeout: float = 35.0) -> dict | None:
    """Spawn the grok TUI in a pty, run `/usage show`, scrape credit usage.
    Linux-only (pty/termios); imports are lazy so this module still loads elsewhere."""
    try:
        import pty
        import select
        import fcntl
        import termios
        import struct
        import pyte
    except Exception:
        return None
    cols, rows = 180, 50
    try:
        master, slave = pty.openpty()
    except Exception:
        return None
    proc = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(cols), LINES=str(rows))
        try:
            _PROBE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        proc = subprocess.Popen(
            [GROK_BIN, "--cwd", str(_PROBE_DIR)],
            stdin=slave, stdout=slave, stderr=slave,
            env=env, preexec_fn=os.setsid, close_fds=True)
        os.close(slave)
        slave = -1
        screen = pyte.Screen(cols, rows)
        stream = pyte.ByteStream(screen)
        start = time.time()
        sent_at = 0.0
        result = None

        def feed(t):
            end = time.time() + t
            while time.time() < end:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        d = os.read(master, 65536)
                    except OSError:
                        return
                    if not d:
                        return
                    stream.feed(d)

        while time.time() - start < timeout:
            feed(0.4)
            text = "\n".join(screen.display)
            if not sent_at and ("always-approve" in text or "Shift+Tab" in text):
                time.sleep(0.4)
                os.write(master, b"/usage show\r")
                sent_at = time.time()
                continue
            if sent_at:
                m = re.search(r"Credits used:\s*([0-9.]+)\s*%", text)
                if m:
                    result = {"pct": float(m.group(1)), "creditsUsed": m.group(1) + "%"}
                    rs = re.search(r"Resets:\s*([^\n│]+?)\s*(?:│|$)", text, re.M)
                    if rs:
                        result["resets"] = rs.group(1).strip()
                    pg = re.search(r"Pay as you go:\s*([^\n│]+?)\s*(?:│|$)", text, re.M)
                    if pg:
                        result["payg"] = pg.group(1).strip()
                    break
        return result
    except Exception:
        return None
    finally:
        try:
            if proc is not None:
                os.write(master, b"\x03")
        except OSError:
            pass
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            try:
                proc.wait(timeout=4)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        for fdc in (master, slave):
            try:
                if fdc and fdc >= 0:
                    os.close(fdc)
            except OSError:
                pass
        _wipe_probe_sessions()


async def _refresh_credits(force: bool = False):
    now = time.time()
    if _credits_cache["loading"]:
        return
    if not force and _credits_cache["data"] and now - _credits_cache["ts"] < CREDITS_TTL:
        return
    _credits_cache["loading"] = True
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _scrape_credits)
        if data:
            _credits_cache["data"] = data
            _credits_cache["ts"] = time.time()
    except Exception:
        pass
    finally:
        _credits_cache["loading"] = False


@app.get("/api/credits")
async def api_credits():
    """Plan/credit usage as shown by grok's `/usage show` (scraped + cached)."""
    now = time.time()
    data = _credits_cache["data"]
    age = (now - _credits_cache["ts"]) if _credits_cache["ts"] else None
    stale = (age is None) or (age > CREDITS_TTL)
    if stale and not _credits_cache["loading"]:
        asyncio.create_task(_refresh_credits())
    out = {"available": bool(data), "loading": _credits_cache["loading"],
           "age": round(age, 1) if age is not None else None}
    if data:
        out.update(data)
    return out


def _drives():
    if os.name == "nt":
        out = []
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            if os.path.exists(d):
                out.append(d)
        return out
    # POSIX / WSL: roots, home, and mounted drives under /mnt
    out = ["/", str(Path.home())]
    mnt = Path("/mnt")
    if mnt.is_dir():
        for child in sorted(mnt.iterdir()):
            try:
                if child.is_dir():
                    out.append(str(child))
            except OSError:
                continue
    seen, res = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


@app.get("/api/browse")
async def api_browse(path: str = ""):
    p = Path(path) if path else cur()
    if not p.exists() or not p.is_dir():
        return {"ok": False, "path": str(p), "parent": None, "drives": _drives(), "dirs": []}
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir() and not child.name.startswith("."):
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else None
    return {"ok": True, "path": str(p), "parent": parent, "drives": _drives(), "dirs": dirs}


def _reschedule_watch():
    obs = getattr(app.state, "observer", None)
    if obs:
        try:
            obs.unschedule_all()
            obs.schedule(_MediaHandler(), str(cur()), recursive=True)
        except Exception:
            pass


@app.get("/api/folder")
async def api_get_folder():
    return {"path": str(cur())}


@app.post("/api/folder")
async def api_set_folder(request: Request):
    body = await request.json()
    raw = (body.get("path") or "").strip().strip('"')
    p = Path(raw)
    if not raw or not p.exists() or not p.is_dir():
        return JSONResponse({"ok": False, "error": "폴더를 찾을 수 없습니다: " + raw}, status_code=400)
    state["folder"] = p.resolve()
    _reschedule_watch()
    return {"ok": True, "path": str(cur())}


def _is_excluded_dir(name: str) -> bool:
    # frame dumps (original_frames, kidol_frames, ...), temp/work dirs, app internals
    return name in EXCLUDE_DIRS or name == "frames" or name.endswith("_frames")


# obvious per-frame dump files (frame_0001.jpg, frame-12.png, frame 3.jpg) — never a real deliverable
_FRAME_FILE_RE = re.compile(r"^frame[-_ ]?\d+\.[a-z0-9]+$", re.IGNORECASE)


def _iter_media(base: Path):
    if not base.exists():
        return
    for root, dirs, files in os.walk(base):
        rootp = Path(root)
        # prune excluded dirs in-place so we never descend into frame dumps / temp / app dir
        dirs[:] = [d for d in dirs if not _is_excluded_dir(d) and (rootp / d).resolve() != APP_DIR]
        for f in files:
            if os.path.splitext(f)[1].lower() not in MEDIA_EXTS:
                continue
            if _FRAME_FILE_RE.match(f):
                continue
            p = rootp / f
            try:
                rel = p.relative_to(base)
            except ValueError:
                continue
            yield p, rel


def _file_hash(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _dedupe_workspace_media(base: Path) -> list[str]:
    """Remove byte-identical duplicate media, keeping one canonical copy (the
    shallowest path). SAFE: a file is deleted only when an identical-content copy
    is retained — content is never lost. Files under uploads/ are never deleted."""
    by_size: dict[int, list[Path]] = {}
    for p, _rel in _iter_media(base):
        try:
            by_size.setdefault(p.stat().st_size, []).append(p)
        except OSError:
            pass
    removed: list[str] = []
    for _size, paths in by_size.items():
        if len(paths) < 2:
            continue
        seen: dict[str, Path] = {}
        for p in sorted(paths, key=lambda x: (len(x.relative_to(base).parts), len(str(x)), str(x))):
            try:
                h = _file_hash(p)
            except OSError:
                continue
            if h not in seen:
                seen[h] = p          # first (shallowest) copy is the keeper
                continue
            try:
                relposix = p.relative_to(base).as_posix()
            except ValueError:
                continue
            if relposix.split("/", 1)[0] == "uploads":
                continue             # never delete user uploads
            try:
                p.unlink()
                removed.append(relposix)
                parent = p.parent    # tidy the now-empty subdir grok created for the copy
                if parent != base and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
    return removed


@app.get("/api/media")
async def api_media(limit: int = 400):
    base = cur()
    items = []
    for p, rel in _iter_media(base):
        try:
            st = p.stat()
        except OSError:
            continue
        ext = p.suffix.lower()
        # version tag busts browser cache when grok rewrites a file under the same
        # name (same prompt → same filename) — otherwise the old video/thumb is shown
        ver = f"{int(st.st_mtime)}-{st.st_size}"
        item = {"name": p.name, "rel": rel.as_posix(),
                "url": "/media/" + quote(rel.as_posix()) + "?v=" + ver,
                "kind": "video" if ext in VIDEO_EXTS else "image",
                "ext": ext, "size": st.st_size, "mtime": st.st_mtime}
        if ext in VIDEO_EXTS:
            item["thumb"] = "/thumb/" + quote(rel.as_posix()) + "?v=" + ver
        items.append(item)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items[:limit], "count": len(items)}


def _safe_media_path(rel: str) -> Path | None:
    base = cur()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file() or target.suffix.lower() not in MEDIA_EXTS:
        return None
    return target


def _thumb_for(path: Path) -> Path | None:
    """Extract + cache a JPEG poster frame for a video (server-side, reliable)."""
    if cv2 is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = hashlib.md5(f"{path}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8")).hexdigest()
    out = THUMBS_DIR / (key + ".jpg")
    if out.exists():
        return out
    try:
        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        target = int(min(fps * 0.3, 8))
        if target > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None
        h, w = frame.shape[:2]
        scale = 360.0 / max(h, w)
        if scale < 1:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return out if out.exists() else None
    except Exception:
        return None


@app.get("/thumb/{rel:path}")
async def thumb_file(rel: str):
    path = _safe_media_path(rel)
    if path is None or path.suffix.lower() not in VIDEO_EXTS:
        return JSONResponse({"error": "not found"}, status_code=404)
    t = _thumb_for(path)
    if t is None:
        return JSONResponse({"error": "no thumbnail"}, status_code=404)
    return FileResponse(t, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/media/{rel:path}")
async def media_file(rel: str, request: Request):
    path = _safe_media_path(rel)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    file_size = path.stat().st_size
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes="):
        s_s, _, e_s = rng[6:].partition("-")
        start = int(s_s) if s_s else 0
        end = int(e_s) if e_s else file_size - 1
        end = min(end, file_size - 1)
        start = min(start, end)
        length = end - start + 1

        def _iter():
            with open(path, "rb") as f:
                f.seek(start)
                rem = length
                while rem > 0:
                    chunk = f.read(min(65536, rem))
                    if not chunk:
                        break
                    rem -= len(chunk)
                    yield chunk

        return StreamingResponse(_iter(), status_code=206, media_type=ctype, headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes", "Content-Length": str(length)})
    return FileResponse(path, media_type=ctype, headers={"Accept-Ranges": "bytes"})


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "upload.bin").replace("\\", "_")
    dest = cur() / name
    if dest.exists():
        dest = cur() / f"{time.strftime('%H%M%S')}_{name}"
    dest.write_bytes(await file.read())
    rel = dest.name
    is_media = dest.suffix.lower() in MEDIA_EXTS
    return {"ok": True, "name": dest.name, "rel": rel, "winpath": str(dest),
            "url": ("/media/" + quote(rel)) if is_media else None,
            "kind": ("video" if dest.suffix.lower() in VIDEO_EXTS else "image") if is_media else "file"}


@app.post("/api/delete")
async def api_delete(request: Request):
    body = await request.json()
    rels = body.get("rels")
    if rels is None and body.get("rel"):
        rels = [body["rel"]]
    rels = rels or []
    deleted, failed = [], []
    for rel in rels:
        path = _safe_media_path(rel)
        if path is None:
            failed.append(rel)
            continue
        try:
            path.unlink()
            deleted.append(rel)
        except OSError:
            failed.append(rel)
    if not deleted and failed:
        return JSONResponse({"ok": False, "error": "delete failed", "failed": failed}, status_code=500)
    return {"ok": True, "deleted": deleted, "failed": failed}


@app.post("/api/rename")
async def api_rename(request: Request):
    body = await request.json()
    src = _safe_media_path(body.get("rel") or "")
    if src is None:
        return JSONResponse({"ok": False, "error": "원본 파일을 찾을 수 없어요"}, status_code=404)
    newname = os.path.basename((body.get("name") or "").strip())
    if not newname or newname in (".", "..") or "/" in newname or "\\" in newname:
        return JSONResponse({"ok": False, "error": "이름이 올바르지 않아요"}, status_code=400)
    if not Path(newname).suffix:          # keep the original extension if none given
        newname = newname + src.suffix
    if Path(newname).suffix.lower() not in MEDIA_EXTS:
        newname = Path(newname).stem + src.suffix   # don't let the type change
    dst = src.parent / newname
    try:
        dst.resolve().relative_to(cur())
    except ValueError:
        return JSONResponse({"ok": False, "error": "잘못된 경로"}, status_code=400)
    if dst == src:
        return {"ok": True, "rel": body.get("rel"), "name": src.name}
    if dst.exists():
        return JSONResponse({"ok": False, "error": "이미 같은 이름의 파일이 있어요"}, status_code=409)
    try:
        src.rename(dst)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "rel": dst.relative_to(cur()).as_posix(), "name": dst.name}


@app.post("/api/zip")
async def api_zip(request: Request):
    body = await request.json()
    rels = body.get("rels") or []
    items = []
    for rel in rels:
        p = _safe_media_path(rel)
        if p is not None:
            items.append((p, os.path.basename(rel)))
    if not items:
        return JSONResponse({"ok": False, "error": "no files"}, status_code=400)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        used = set()
        for p, name in items:
            arc, i = name, 1
            while arc in used:
                stem, dot, ext = name.rpartition(".")
                arc = (f"{stem}_{i}.{ext}" if dot else f"{name}_{i}")
                i += 1
            used.add(arc)
            try:
                zf.write(p, arc)
            except OSError:
                pass
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="grok-studio.zip"'})


# ---------------------------------------------------------------------------
# Grok runner — runs as a background JOB decoupled from the WebSocket, so a
# page refresh or dropped socket neither kills nor loses the run. Clients
# subscribe by sid: they get a snapshot of progress so far + live events.
# ---------------------------------------------------------------------------
async def _broadcast(job: dict, evt: dict):
    for ws in list(job["subscribers"]):
        try:
            await ws.send_json(evt)
        except Exception:
            job["subscribers"].discard(ws)


def _snapshot(job: dict) -> dict:
    return {"type": "snapshot", "job": {
        "id": job["id"], "prompt": job["prompt"], "thought": job["thought"],
        "answer": job["answer"], "status": job["status"], "outcome": job.get("outcome"),
        "code": job.get("code"), "stopReason": job.get("stopReason"),
        "harvested": job.get("harvested", []), "stderr": job.get("stderr", ""),
        "cmd": job.get("cmd", ""),
        "elapsed": time.time() - job["started"],
    }}


async def _heartbeat(job: dict):
    while job["status"] == "running":
        await asyncio.sleep(5)
        if job["status"] != "running":
            break
        await _broadcast(job, {"type": "heartbeat", "elapsed": time.time() - job["started"]})


async def _post_run(job: dict, proc, folder: Path) -> None:
    """Deferred post-run cleanup that must NOT delay run_end. Reaps any lingering grok
    child, then dedupes the workspace off the event loop and nudges the gallery — so
    clearing '생각 중' / re-enabling send no longer waits on md5-hashing multi-MB videos."""
    try:
        if proc.returncode is None:
            # grok just emitted `end`; let it exit cleanly (it's flushing session state
            # used by --resume). Only force it if it actually lingers. This wait is in the
            # background now, so it never holds the "생각 중" / send-disabled state.
            try:
                await asyncio.wait_for(proc.wait(), timeout=12)
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
    except Exception:
        pass
    if _env("GROK_WEB_DEDUPE", "1").lower() not in ("0", "false", "no"):
        try:
            loop = asyncio.get_event_loop()
            deduped = await loop.run_in_executor(None, _dedupe_workspace_media, folder)
        except Exception:
            deduped = []
        if deduped:
            job["deduped"] = deduped
            try:
                await _broadcast(job, {"type": "dedup", "deduped": deduped})
            except Exception:
                pass


async def _run_job(job: dict):
    sid = job["sid"]
    folder = job["folder"]
    opts = job.get("opts") or {}
    model = opts.get("model") or MODEL
    rules = INTERMEDIATE_RULE + "\n\n" + FAST_FINISH_RULE
    if IMG2VIDEO_BIN.exists() and VIDEOEXTEND_BIN.exists():
        rules = rules + "\n\n" + MEDIA_ROUTING_RULE
    _extra = (opts.get("rules") or "").strip()
    if _extra:
        rules = rules + "\n\n" + _extra
    cmd = [GROK_BIN, "-p", job["prompt"], "--output-format", "streaming-json",
           "--cwd", folder, "--always-approve", "-m", model, "--rules", rules]
    # --no-subagents kills the post-task verifier subagent (what keeps the UI "thinking" long
    # after the deliverable). grok REJECTS it together with --check or --best-of-n (both rely on
    # subagents), so only add it when neither is on — otherwise grok exits 2 on arg parse.
    if not opts.get("check") and not opts.get("bestOfN"):
        cmd += ["--no-subagents"]
    if opts.get("effort"):
        cmd += ["--effort", str(opts["effort"])]
    _mt = opts.get("maxTurns") or MAX_TURNS
    if _mt:
        cmd += ["--max-turns", str(_mt)]
    if opts.get("bestOfN"):
        cmd += ["--best-of-n", str(opts["bestOfN"])]
    if opts.get("check"):
        cmd += ["--check"]
    if opts.get("webSearch") is False:
        cmd += ["--disable-web-search"]
    if opts.get("customFlags"):
        try:
            cmd += shlex.split(str(opts["customFlags"]))
        except Exception:
            pass
    if grok_sessions.get(sid):
        cmd += ["--resume", grok_sessions[sid]]

    def _disp(a):
        if a == job["prompt"]:
            return '"<요청 프롬프트>"'
        if a == rules:
            return '"<규칙>"'
        if a == GROK_BIN:
            return "grok"
        return ('"' + (a if len(a) <= 48 else a[:46] + "…") + '"') if " " in a else a
    cmd_display = " ".join(_disp(a) for a in cmd)
    job["cmd"] = cmd_display
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=folder)
    except FileNotFoundError:
        job.update({"status": "done", "outcome": "error"})
        await _broadcast(job, {"type": "run_end", "code": -1, "outcome": "error",
                               "harvested": [], "stderr": f"grok를 찾을 수 없습니다: {GROK_BIN}"})
        return
    job["proc"] = proc
    hb = asyncio.create_task(_heartbeat(job))
    await _broadcast(job, {"type": "run_start", "folder": folder, "cmd": cmd_display})
    # video ops (img2video / videoextend) run a long blocking CLI with no stdout for minutes;
    # keep the inactivity window generous so they aren't killed mid-generation
    inactivity = float(_env("GROK_WEB_INACTIVITY_TIMEOUT", "600"))
    got_text = False
    stop_reason = None
    timed_out = False
    saw_end = False
    rc = 0
    err = ""
    try:
        assert proc.stdout is not None
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=inactivity)
            except asyncio.TimeoutError:
                timed_out = True
                break
            if not raw:
                break  # EOF -> process finished
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                await _broadcast(job, {"type": "raw", "data": line})
                continue
            et = evt.get("type")
            if et == "thought":
                job["thought"] += evt.get("data", "")
            elif et == "text":
                d = evt.get("data", "")
                if d:
                    got_text = True
                job["answer"] += d
            elif et == "end":
                stop_reason = evt.get("stopReason")
                if evt.get("sessionId"):
                    grok_sessions[sid] = evt["sessionId"]
                saw_end = True
                await _broadcast(job, evt)
                break  # turn complete — do NOT wait for stdout EOF (a lingering child can hold it)
            await _broadcast(job, evt)
    except Exception as ex:
        err = (err + f"\nrunner error: {ex}").strip()
    # ── finalize ── run_end must fire the instant the turn ends; slow cleanup is deferred.
    hb.cancel()
    # Only the non-success paths need grok's real exit code / stderr (for the error panel).
    # The success path (saw_end) must NOT wait on a possibly-lingering grok child — that
    # wait plus the dedupe pass was the ~3s tail between "media appeared" and "생각 중" clearing.
    if not saw_end and not job.get("cancelled") and not timed_out:
        if proc.returncode is None:
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=4)
            except asyncio.TimeoutError:
                rc = 0
        else:
            rc = proc.returncode or 0
        if proc.stderr is not None:
            try:
                err = (await asyncio.wait_for(proc.stderr.read(), timeout=3)).decode("utf-8", "replace")
            except Exception:
                pass
    else:
        rc = proc.returncode or 0
    try:
        harvested = _harvest_session_media(grok_sessions.get(sid, ""), Path(folder))
    except Exception:
        harvested = []
    if job.get("cancelled"):
        outcome = "cancelled"
    elif timed_out:
        outcome = "timeout"
    elif saw_end:
        outcome = "ok" if (got_text or harvested) else "no_output"
    elif rc:
        outcome = "error"
    elif got_text or harvested:
        outcome = "ok"
    else:
        outcome = "no_output"
    job.update({"status": "done", "outcome": outcome, "code": rc,
                "stopReason": stop_reason, "harvested": [p.name for p in harvested],
                "deduped": [], "stderr": err[-2000:], "ended": time.time()})
    await _broadcast(job, {"type": "run_end", "code": rc, "outcome": outcome,
                           "stopReason": stop_reason, "harvested": job["harvested"],
                           "deduped": [], "stderr": job["stderr"]})
    # Deferred: reap any lingering grok child + dedupe the workspace off the event loop,
    # then nudge the gallery. None of this blocks the send button / "생각 중" clear above.
    asyncio.create_task(_post_run(job, proc, Path(folder)))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _is_authed(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            sid = msg.get("sid") or "default"
            if action == "subscribe":
                job = jobs.get(sid_job.get(sid, ""))
                if job:
                    job["subscribers"].add(ws)
                    await ws.send_json(_snapshot(job))
                else:
                    await ws.send_json({"type": "no_job"})
            elif action == "prompt":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                existing = jobs.get(sid_job.get(sid, ""))
                if existing and existing["status"] == "running":
                    await ws.send_json({"type": "busy"})
                    continue
                old = sid_job.get(sid)
                if old and old in jobs:
                    jobs.pop(old, None)  # keep only the latest job per sid
                jid = secrets.token_hex(6)
                job = {"id": jid, "sid": sid, "prompt": text, "status": "running",
                       "thought": "", "answer": "", "started": time.time(),
                       "folder": str(cur()), "opts": (msg.get("opts") or {}), "subscribers": {ws}}
                jobs[jid] = job
                sid_job[sid] = jid
                asyncio.create_task(_run_job(job))
            elif action == "cancel":
                job = jobs.get(sid_job.get(sid, ""))
                if job and job["status"] == "running":
                    job["cancelled"] = True
                    proc = job.get("proc")
                    if proc and proc.returncode is None:
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                    await _broadcast(job, {"type": "cancelled"})
            elif action == "reset":
                grok_sessions.pop(sid, None)
                await ws.send_json({"type": "reset_ok"})
            elif action == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        for j in jobs.values():
            j["subscribers"].discard(ws)


# ---------------------------------------------------------------------------
# Watcher -> broadcast
# ---------------------------------------------------------------------------
class _MediaHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        if getattr(event, "is_directory", False):
            return
        src = getattr(event, "src_path", "") or getattr(event, "dest_path", "")
        if Path(src).suffix.lower() in MEDIA_EXTS and _loop and _media_dirty:
            _loop.call_soon_threadsafe(_media_dirty.set)


async def _media_broadcaster():
    assert _media_dirty is not None
    while True:
        await _media_dirty.wait()
        _media_dirty.clear()
        await asyncio.sleep(0.6)
        for ws in list(clients):
            try:
                await ws.send_json({"type": "media_changed"})
            except Exception:
                clients.discard(ws)


async def _media_poller():
    """Periodic re-scan — reliable on filesystems without inotify (e.g. /mnt/c in WSL)."""
    prev = None
    while True:
        await asyncio.sleep(3)
        try:
            sig = tuple(sorted(
                (rel.as_posix(), int(p.stat().st_mtime), p.stat().st_size)
                for p, rel in _iter_media(cur())
            ))
        except Exception:
            continue
        if prev is not None and sig != prev and _media_dirty is not None:
            _media_dirty.set()
        prev = sig


@app.on_event("startup")
async def _startup():
    global _media_dirty, _loop
    _loop = asyncio.get_running_loop()
    _media_dirty = asyncio.Event()
    # inotify does not work on mounted drives (/mnt/c in WSL) — fall back to polling there
    _poll = _env("GROK_WEB_POLL_WATCH", "0").lower() not in ("0", "false", "no", "") \
        or str(cur()).startswith("/mnt/")
    app.state.observer = None
    if _poll:
        # mounted drive (/mnt/c): inotify unavailable -> periodic re-scan
        asyncio.create_task(_media_poller())
    else:
        observer = Observer()
        observer.schedule(_MediaHandler(), str(cur()), recursive=True)
        observer.daemon = True
        observer.start()
        app.state.observer = observer
    asyncio.create_task(_media_broadcaster())
    asyncio.create_task(_refresh_credits(force=True))  # warm the credit-usage cache
    print("=" * 60)
    print(f"  Grok Studio  ·  http://{HOST}:{PORT}")
    print(f"  folder   : {cur()}")
    print(f"  password : {PASSWORD}")
    print("=" * 60)


@app.on_event("shutdown")
async def _shutdown():
    obs = getattr(app.state, "observer", None)
    if obs:
        obs.stop()
