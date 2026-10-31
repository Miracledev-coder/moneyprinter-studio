import re
"""MoneyPrinter Studio: a clean, private front end for MoneyPrinterTurbo.
Keys come from env only. One job at a time; temp files are cleaned after each job."""
import hashlib, hmac, os, queue, secrets, shutil, sys, threading, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def write_config():
    import toml
    cfg = toml.load(ROOT / "config.example.toml")
    a = cfg.setdefault("app", {})
    a.update(video_source="pixabay", llm_provider="gemini",
             pixabay_api_keys=[os.environ.get("PIXABAY_API_KEY", "")],
             gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
             gemini_model_name=GEMINI_MODEL, subtitle_provider="edge",
             log_level="INFO", max_concurrent_tasks=1, video_clip_concurrency=1, edge_tts_timeout=120)
    (ROOT / "config.toml").write_text(toml.dumps(cfg))
    os.chmod(ROOT / "config.toml", 0o600)


write_config()

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from app.models import const  # noqa: E402
from app.models.schema import VideoParams, VideoAspect  # noqa: E402
from app.services import state as sm, task as tm  # noqa: E402
from app.utils import utils  # noqa: E402


def load_users():
    """APP_USERS='u1:p1,u2:p2' (max 5). Entries with spaces in the username or
    comment-like lines are ignored, so a pasted header line can't become a user."""
    users = {}
    raw = os.environ.get("APP_USERS", "").replace("\n", ",")
    for item in raw.split(","):
        item = item.strip()
        if not item or item.startswith("#") or ":" not in item:
            continue
        u, p = item.split(":", 1)
        u, p = u.strip(), p.strip()
        if u and p and " " not in u and len(p) >= 8:
            users[u] = p
    return dict(list(users.items())[:5])


USERS = load_users()
SECRET = (os.environ.get("SESSION_SECRET") or secrets.token_hex(32)).encode()
OUT_DIR = Path(os.environ.get("STUDIO_OUT", "/tmp/studio_out")); OUT_DIR.mkdir(parents=True, exist_ok=True)
KEEP_VIDEOS = 10

VOICES = {
    "Ezinne — Nigerian, female": "en-NG-EzinneNeural-Female",
    "Abeo — Nigerian, male": "en-NG-AbeoNeural-Male",
    "Sonia — British, female": "en-GB-SoniaNeural-Female",
    "Ryan — British, male": "en-GB-RyanNeural-Male",
    "Jenny — American, female": "en-US-JennyNeural-Female",
    "Andrew — American, male": "en-US-AndrewNeural-Male",
    "Aria — American, female": "en-US-AriaNeural-Female",
    "Guy — American, male": "en-US-GuyNeural-Male",
}
EDGE_VOICES = dict(VOICES)
ELEVEN_VOICES = {
    "Sarah (ElevenLabs) — American, female": "elevenlabs:EXAVITQu4vr4xnSDxMaL:Sarah",
    "Jessica (ElevenLabs) — American, female": "elevenlabs:cgSgspJ2msm6clMCkdW9:Jessica",
    "Alice (ElevenLabs) — British, female": "elevenlabs:Xb7hH8MSUJpSbSDYk0k2:Alice",
    "Liam (ElevenLabs) — American, male": "elevenlabs:TX3LPaxmHKxFdv7VOQHJ:Liam",
    "Brian (ElevenLabs) — American, male": "elevenlabs:nPczCjzI2devNBz1zQrb:Brian",
    "George (ElevenLabs) — British, male": "elevenlabs:JBFqnCBsd6RMkjVDRZzb:George",
}
if os.environ.get("ELEVENLABS_API_KEY"):
    VOICES.update(ELEVEN_VOICES)
FONTS = {f.name for f in (Path(__file__).resolve().parent.parent / "resource" / "fonts").glob("*.tt[fc]")}
STYLES = {
    "classic": dict(text_fore_color="#FFFFFF", stroke_color="#000000", stroke_width=1.5, text_background_color=False),
    "bold": dict(text_fore_color="#FFE14D", stroke_color="#000000", stroke_width=2.5, text_background_color=False),
    "boxed": dict(text_fore_color="#FFFFFF", stroke_color="#000000", stroke_width=0, text_background_color="#000000AA",
                  rounded_subtitle_background=True),
}

jobs, jobq = {}, queue.Queue()
HIST = OUT_DIR / "history.json"
META = ("status", "user", "title", "aspect", "voice", "created", "elapsed", "error", "video")
try:
    import json as _json
    for k, v in _json.loads(HIST.read_text()).items():
        if v.get("status") == "done" and Path(v.get("video", "")).exists():
            jobs[k] = v
except Exception:
    pass


def save_history():
    import json
    done = {k: {m: v.get(m) for m in META} for k, v in jobs.items() if v.get("status") in ("done", "error")}
    HIST.write_text(json.dumps(done))


def sign(u):
    return u + "|" + hmac.new(SECRET, u.encode(), hashlib.sha256).hexdigest()


def current_user(req: Request):
    c = req.cookies.get("mps", "")
    u = c.split("|", 1)[0]
    return u if u in USERS and hmac.compare_digest(c, sign(u)) else None


def worker():
    while True:
        jid = jobq.get()
        j = jobs[jid]
        j.update(status="running", started=time.time())
        tdir = Path(utils.task_dir(jid))
        try:
            for attempt in range(3):  # Gemini sometimes returns a transient 503
                res = tm.start(jid, j["params"], stop_at="video")
                vids = (res or {}).get("videos") or []
                err = str((sm.state.get_task(jid) or {}).get("error") or "")
                if vids or not ("script" in err or "terms" in err):
                    break
                time.sleep(8 * (attempt + 1))
            if not vids:
                t = sm.state.get_task(jid) or {}
                raise RuntimeError(t.get("error") or t.get("message") or "generation failed (no video)")
            dest = OUT_DIR / f"{jid}.mp4"
            shutil.move(vids[0], dest)
            j.update(status="done", video=str(dest), script=(res or {}).get("script", ""))
        except Exception as e:  # noqa: BLE001
            j.update(status="error", error=str(e)[:300])
        finally:
            j["elapsed"] = round(time.time() - j["started"])
            shutil.rmtree(tdir, ignore_errors=True)
            shutil.rmtree(ROOT / "storage" / "cache_videos", ignore_errors=True)
            try:
                sm.state.delete_task(jid)
            except Exception:
                pass
            old = sorted(OUT_DIR.glob("*.mp4"), key=os.path.getmtime)[:-KEEP_VIDEOS]
            for f in old:
                f.unlink(missing_ok=True)
            for k in [k for k, v in jobs.items() if v.get("status") == "done" and not Path(v["video"]).exists()]:
                jobs.pop(k, None)
            j.pop("params", None)
            save_history()
            import gc; gc.collect()
            try:
                import ctypes; ctypes.CDLL("libc.so.6").malloc_trim(0)
            except Exception:
                pass
            jobq.task_done()


threading.Thread(target=worker, daemon=True).start()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
PAGE = ((HERE / "index.html").read_text()
        .replace("{{SHELL}}", (HERE / "static/cc-shell.html").read_text())
        .replace("{{IDEAS}}", (HERE / "static/cc-ideas.html").read_text())
        .replace("{{HEADER}}", (HERE / "static/cc-header.html").read_text())
        .replace("{{APP}}", "MoneyPrinter Studio"))
LOGIN = (Path(__file__).parent / "login.html").read_text()
LOCKED = "<h2 style='font-family:sans-serif;text-align:center;margin-top:20vh'>🔒 MoneyPrinter Studio is locked.<br><small>The owner must set APP_USERS.</small></h2>"


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(req: Request):
    if not USERS:
        return HTMLResponse(LOCKED, 503)
    if not current_user(req):
        return HTMLResponse(LOGIN.replace("{{ERR}}", ""))
    opts = '<optgroup label="Free voices (Edge)">' + "".join(f'<option value="{v}">{k}</option>' for k, v in EDGE_VOICES.items()) + "</optgroup>"
    if os.environ.get("ELEVENLABS_API_KEY"):
        opts += ('<optgroup label="ElevenLabs (free plan ~10k credits/mo &#8776; 10 min audio; commercial use needs attribution)">'
                 + "".join(f'<option value="{v}">{k}</option>' for k, v in ELEVEN_VOICES.items()) + "</optgroup>")
    return HTMLResponse(PAGE.replace("{{VOICES}}", opts).replace("{{USER}}", current_user(req)))


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    p = USERS.get(username.strip())
    if not p or not hmac.compare_digest(p, password):
        time.sleep(1)
        return HTMLResponse(LOGIN.replace("{{ERR}}", "Wrong username or password."), 401)
    r = RedirectResponse("/", 303)
    r.set_cookie("mps", sign(username.strip()), httponly=True, samesite="lax", max_age=7 * 86400,
                 secure=os.environ.get("RENDER") is not None)
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/", 303); r.delete_cookie("mps"); return r


@app.post("/api/generate")
async def generate(req: Request):
    user = current_user(req)
    if not user:
        return JSONResponse({"error": "not logged in"}, 401)
    b = await req.json()
    topic, script = (b.get("topic") or "").strip()[:300], (b.get("script") or "").strip()[:4000]
    if not topic and not script:
        return JSONResponse({"error": "Enter a topic or a script."}, 400)
    if sum(1 for j in jobs.values() if j["status"] in ("queued", "running")) >= 5:
        return JSONResponse({"error": "Queue is full, try again in a few minutes."}, 429)
    hexc = lambda v, d: v if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v) else d
    if b.get("sub_style") == "custom":
        style = dict(text_fore_color=hexc(b.get("sub_color"), "#FFFFFF"), stroke_color=hexc(b.get("sub_stroke"), "#000000"),
                     stroke_width=max(0.0, min(float(b.get("sub_stroke_w") or 0), 8.0)),
                     text_background_color=(hexc(b.get("sub_bg"), "#000000") + "AA") if b.get("sub_bg_on") else False,
                     rounded_subtitle_background=bool(b.get("sub_bg_on")))
    else:
        style = dict(STYLES.get(b.get("sub_style"), STYLES["classic"]))
    font = b.get("sub_font") if b.get("sub_font") in FONTS else "BeVietnamPro-Bold.ttf"
    pos = b.get("sub_pos") if b.get("sub_pos") in ("top", "center", "bottom", "custom") else "bottom"
    mode = "word_by_word" if b.get("sub_mode") == "word_by_word" else "sentence"
    preset = b.get("preset") if b.get("preset") in ("highlight", "karaoke", "boldpop", "neon", "chunk") else ""
    if preset:
        mode = "word_by_word"  # presets regroup word timings into ASS lines
    extra = dict(subtitle_preset=preset, highlight_color=hexc(b.get("sub_hl"), "#A855F7"),
                 subtitle_uppercase=bool(b.get("sub_upper")))
    voice = b.get("voice") if b.get("voice") in VOICES.values() else VOICES["Ezinne — Nigerian, female"]
    length = {"short": 1, "medium": 2, "long": 3}.get(b.get("length"), 1)
    words = {1: "35 to 45 words (about 15 seconds spoken)", 2: "90 to 110 words", 3: "170 to 200 words"}[length]
    params = VideoParams(
        video_subject=topic or script[:80], video_script=script,
        video_terms=[t.strip()[:40] for t in str(b.get("terms") or "").split(",") if t.strip()][:6] or None,
        video_aspect=VideoAspect.landscape if b.get("aspect") == "16:9" else VideoAspect.portrait,
        video_clip_duration=max(2, min(int(b.get("clip") or 4), 8)), video_count=1, video_source="pixabay",
        video_language="en", voice_name=voice, voice_rate=float(b.get("rate") or 1.0),
        bgm_type="random" if b.get("music", True) else "", bgm_volume=0.15,
        subtitle_enabled=bool(b.get("subtitles", True)), font_name=font,
        font_size=max(16, min(int(b.get("font_size") or 48), 120)), subtitle_position=pos,
        custom_position=max(0.0, min(float(b.get("sub_custom") or 70), 100.0)), subtitle_display_mode=mode,
        n_threads=1, paragraph_number=length,
        video_script_prompt=f"Write in English. Keep the whole script to {words}.", **style, **extra)
    jid = str(uuid.uuid4())
    vlabel = next((k.split(" —")[0] for k, v in VOICES.items() if v == voice), "")
    jobs[jid] = dict(status="queued", user=user, params=params, created=time.time(), title=(topic or script)[:90],
                     aspect="16:9" if b.get("aspect") == "16:9" else "9:16", voice=vlabel)
    jobq.put(jid)
    return {"id": jid}


sys.path.insert(0, str(HERE))
import ideas as idealab  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402


@app.post("/api/research")
async def api_research(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    t = ((await req.json()).get("topic") or "").strip()
    if not t:
        return JSONResponse({"error": "Enter a niche or topic."}, 400)
    return {"sources": await run_in_threadpool(idealab.research, t)}


@app.post("/api/ideas")
async def api_ideas(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    b = await req.json()
    if not (b.get("topic") or "").strip():
        return JSONResponse({"error": "Enter a niche or topic."}, 400)
    try:
        res = b.get("research") or await run_in_threadpool(idealab.research, b["topic"])
        return {"ideas": await run_in_threadpool(idealab.ideas, b, res)}
    except Exception as e:
        return JSONResponse({"error": "Idea generation failed: " + str(e)[:120]}, 502)


@app.post("/api/script")
async def api_script(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        return await run_in_threadpool(idealab.script, await req.json())
    except Exception as e:
        return JSONResponse({"error": "Script writing failed: " + str(e)[:120]}, 502)


@app.get("/api/status/{jid}")
def status(jid: str, req: Request):
    j = jobs.get(jid)
    if not current_user(req) or not j:
        return JSONResponse({"error": "not found"}, 404)
    out = {k: j.get(k) for k in ("status", "error", "elapsed", "script")}
    if j["status"] == "queued":
        out["ahead"] = sum(1 for k, x in jobs.items() if x["status"] in ("queued", "running") and x["created"] < j["created"])
    if j["status"] == "running":
        t = sm.state.get_task(jid) or {}
        out["progress"] = int(t.get("progress") or 0)
        out["elapsed"] = round(time.time() - j["started"])
    return out


@app.get("/api/history")
def history(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    out = []
    for k, j in sorted(jobs.items(), key=lambda kv: -kv[1]["created"]):
        if j.get("user") != u:
            continue
        d = {m: j.get(m) for m in ("status", "title", "aspect", "voice", "elapsed", "error")}
        d["id"] = k
        d.update({x: y for x, y in status(k, req).items() if x in ("progress", "elapsed", "ahead")} if j["status"] in ("queued", "running") else {})
        out.append(d)
    return out[:30]


@app.get("/video/{jid}")
def video(jid: str, req: Request, dl: int = 0):
    j = jobs.get(jid)
    if not j or current_user(req) != j.get("user") or j.get("status") != "done" or not Path(j["video"]).exists():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(j["video"], media_type="video/mp4",
                        filename=f"moneyprinter-{jid[:8]}.mp4" if dl else None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
