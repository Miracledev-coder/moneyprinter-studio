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
             log_level="INFO", max_concurrent_tasks=1, video_clip_concurrency=1, edge_tts_timeout=300)
    (ROOT / "config.toml").write_text(toml.dumps(cfg))
    os.chmod(ROOT / "config.toml", 0o600)


write_config()

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from app.models import const  # noqa: E402
from app.models.schema import VideoParams, VideoAspect, MaterialInfo  # noqa: E402
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
_SECRETS = [v for k in ("ELEVENLABS_API_KEY", "RENDER_API_KEY", "GEMINI_API_KEY", "PIXABAY_API_KEY", "HF_TOKEN",
                        "SESSION_SECRET", "PEXELS_API_KEY", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN") if len(v := os.environ.get(k, "")) >= 6]
_SECRETS += [p for p in USERS.values()]


def redact(x):
    """Strip any secret value (API keys, passwords, key=... query params) from text shown to users or logs."""
    x = str(x)
    for v in _SECRETS:
        x = x.replace(v, "***")
    return re.sub(r"(?i)((?:api_?)?key=)[^&\s'\"]+", r"\1***", x)


try:
    from loguru import logger as _lg
    _lg.configure(patcher=lambda r: r.update(message=redact(r["message"])))
except Exception:
    pass

# Per-user daily caps (in memory; reset at UTC midnight or on restart). Protects the shared free API keys.
CAPS = {"videos": int(os.environ.get("CAP_VIDEOS", 12)), "ai": int(os.environ.get("CAP_AI", 60)),
        "eleven_chars": int(os.environ.get("CAP_ELEVEN_CHARS", 3000)), "previews": 200,
        "minutes": int(os.environ.get("CAP_MINUTES", 30))}
_usage = {}


def use(user, kind, n=1):
    """Count n units of `kind` for today; return False (and count nothing) if it would pass the cap."""
    k = (user, kind, time.strftime("%Y-%m-%d", time.gmtime()))
    if _usage.get(k, 0) + n > CAPS[kind]:
        return False
    _usage[k] = _usage.get(k, 0) + n
    return True


def num(v, d, lo, hi, cast=float):
    try:
        return max(lo, min(cast(float(v)), hi))
    except (TypeError, ValueError):
        return d


def clean_text(t, n):
    """User text that becomes subtitles: drop ASS/libass override chars and control chars."""
    t = str(t or "")[:n]
    t = t.replace("{", "(").replace("}", ")").replace("\\", "/")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", t).strip()


def clean_body(b, n=300):
    """Cap every string in a JSON body (recursively) so prompts sent to Gemini stay small."""
    if isinstance(b, dict):
        return {str(k)[:40]: clean_body(v, n) for k, v in list(b.items())[:40]}
    if isinstance(b, list):
        return [clean_body(v, n) for v in b[:40]]
    if isinstance(b, str):
        return b[:n]
    return b if isinstance(b, (int, float, bool)) or b is None else None


_fails = {}
SECRET = (os.environ.get("SESSION_SECRET") or secrets.token_hex(32)).encode()
OUT_DIR = Path(os.environ.get("STUDIO_OUT", "/tmp/studio_out")); OUT_DIR.mkdir(parents=True, exist_ok=True)
KEEP_VIDEOS = int(os.environ.get("KEEP_VIDEOS", 24))

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
ELEVEN_VOICES = {  # premade voices: the only ones the free API allows; all work with Eleven v4
    "Sarah (ElevenLabs v4) — American, female": "elevenlabs:EXAVITQu4vr4xnSDxMaL:Sarah",
    "Jessica (ElevenLabs v4) — American, female": "elevenlabs:cgSgspJ2msm6clMCkdW9:Jessica",
    "Lily (ElevenLabs v4) — British, female": "elevenlabs:pFZP5JQG7iQjIQuC4Bku:Lily",
    "Alice (ElevenLabs v4) — British, female": "elevenlabs:Xb7hH8MSUJpSbSDYk0k2:Alice",
    "Brian (ElevenLabs v4) — American, male": "elevenlabs:nPczCjzI2devNBz1zQrb:Brian",
    "Chris (ElevenLabs v4) — American, male": "elevenlabs:iP95p4xoKVk53GoZ742B:Chris",
    "George (ElevenLabs v4) — British, male": "elevenlabs:JBFqnCBsd6RMkjVDRZzb:George",
    "Daniel (ElevenLabs v4) — British, male": "elevenlabs:onwK4e9ZLuTAKqWW03F9:Daniel",
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
META = ("status", "user", "title", "aspect", "voice", "created", "elapsed", "error", "video", "req", "retries", "kind", "part", "yt", "parent", "drive")
sys.path.insert(0, str(Path(__file__).parent))
import gdrive as gd  # noqa: E402


def _load(items):
    for k, v in items:
        if k in jobs or not isinstance(v, dict):
            continue
        if v.get("status") == "done" and (Path(v.get("video") or "").exists() or v.get("drive")):
            jobs[k] = v
        elif v.get("status") in ("queued", "running") and v.get("req"):
            # interrupted by a restart/redeploy: run it again (long jobs resume from the next part)
            if (v.get("retries") or 0) < (6 if v.get("kind") == "long" else 1):
                v.update(status="queued", retries=(v.get("retries") or 0) + 1, created=v.get("created") or time.time())
            else:
                v.update(status="error", error="Stopped by a server restart too many times. Please try again.")
                v.pop("req", None)
            jobs[k] = v
        elif v.get("status") == "error":
            jobs[k] = v


try:
    import json as _json
    _load(_json.loads(HIST.read_text()).items())
except Exception:
    pass
if gd.enabled():  # Render's disk is wiped on restart: rebuild History from Drive
    try:
        _load((gd.get_json("history.json") or {}).items())
        print("history restored from Drive:", len(jobs), "items", flush=True)
    except Exception as e:  # noqa: BLE001
        print("drive history load failed:", type(e).__name__, flush=True)


def save_history():
    import json
    done = {k: {m: v.get(m) for m in META} for k, v in list(jobs.items())}
    HIST.write_text(json.dumps(done))
    if "_up" in globals():
        _up.history_changed()


def _snapshot():
    return {k: {m: v.get(m) for m in META} for k, v in list(jobs.items())}


gd.Uploader.snapshot = staticmethod(_snapshot)
_up = gd.Uploader(jobs, lambda: save_history())


SESSION_DAYS = 30


def sign(u, exp=None, remember=True):
    """Stateless session: user|expiry|remember|HMAC. Survives restarts as long as SESSION_SECRET is fixed."""
    exp = int(exp or time.time() + SESSION_DAYS * 86400)
    body = f"{u}|{exp}|{1 if remember else 0}"
    return body + "|" + hmac.new(SECRET, (body + "|" + hashlib.sha256(USERS.get(u, "").encode()).hexdigest()[:16]).encode(), hashlib.sha256).hexdigest()


def _session(req):
    c = req.cookies.get("mps", "")
    parts = c.split("|")
    if len(parts) != 4:
        return None
    u, exp, rem, _ = parts
    try:
        if u in USERS and int(exp) > time.time() and hmac.compare_digest(c, sign(u, int(exp), rem == "1")):
            return u, int(exp), rem == "1"
    except ValueError:
        pass
    return None


def current_user(req: Request):
    s = _session(req)
    return s[0] if s else None


def set_session(r, u, remember):
    kw = dict(httponly=True, samesite="lax", secure=os.environ.get("RENDER") is not None)
    if remember:
        kw["max_age"] = SESSION_DAYS * 86400
    r.set_cookie("mps", sign(u, remember=remember), **kw)


sys.path.insert(0, str(Path(__file__).parent))
import scenes as scn  # noqa: E402


def render(jid, req, dest):
    """Render one video for `req` into `dest` (blocking). Returns the script used."""
    tdir = Path(utils.task_dir(jid))
    try:
        if req.get("scenes") and not all(Path(p).exists() for p in (req.get("_local") or ["/nonexistent"])):
            req["_local"] = scn.materialize(jid, req.get("_user", ""), req["scenes"], utils.storage_dir("local_videos", create=True))
            if not req["_local"]:
                raise RuntimeError("None of the chosen scene clips could be loaded.")
        for attempt in range(3):  # Gemini sometimes returns a transient 503
            res = tm.start(jid, make_params(req), stop_at="video")
            vids = (res or {}).get("videos") or []
            err = str((sm.state.get_task(jid) or {}).get("error") or "")
            if vids or not ("script" in err or "terms" in err):
                break
            time.sleep(8 * (attempt + 1))
        if not vids:
            t = sm.state.get_task(jid) or {}
            raise RuntimeError(t.get("error") or t.get("message") or "generation failed (no video)")
        shutil.move(vids[0], dest)
        return (res or {}).get("script", "")
    finally:
        shutil.rmtree(tdir, ignore_errors=True)
        for f in Path(utils.storage_dir("local_videos", create=True)).glob(jid[:8] + "_*"):
            f.unlink(missing_ok=True)
        shutil.rmtree(ROOT / "storage" / "cache_videos", ignore_errors=True)
        try:
            sm.state.delete_task(jid)
        except Exception:
            pass
        import gc; gc.collect()
        try:
            import ctypes; ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass


def run_long(lid, j):
    """Render each part in order; completed parts survive restarts (resume from the next part)."""
    import longform as lf
    r = j["req"]
    parts = r["parts"]
    if not r.get("meta"):
        r["meta"] = lf.meta(j["title"], parts); save_history()
    used = set(r.get("used") or [])
    done = r.setdefault("done", [])
    while len(done) < len(parts):
        n = len(done)
        j["part"] = f"Part {n + 1} of {len(parts)}"; save_history()
        pid = f"{lid[:8]}-p{n + 1}-{uuid.uuid4().hex[:6]}"
        preq = {k: v for k, v in r.items() if k not in ("parts", "done", "meta", "used", "scenes", "_local")}
        preq.update(script=parts[n], topic="", _user=j["user"])
        # per-sentence footage, never reusing a clip from an earlier part
        plan = scn.plan(parts[n], r.get("aspect") or "16:9")
        sc = []
        for s in plan:
            o = next((x for x in s["options"] if x["id"] not in used), None)
            if o:
                used.add(o["id"]); sc.append({"src": o["url"]})
        if sc:
            preq["scenes"] = sc
        r["used"] = list(used)
        dest = OUT_DIR / f"{lid}_p{n + 1}.mp4"
        render(pid, preq, dest)
        jobs[pid] = dict(status="done", user=j["user"], title=f"{j['title'][:70]} - Part {n + 1}", aspect=j["aspect"],
                         voice=j["voice"], created=time.time(), elapsed=round(time.time() - j["started"]), video=str(dest),
                         parent=lid)
        done.append(pid); save_history(); _up.push(pid)
    j["part"] = "Joining parts"; save_history()
    files = []
    for p in done:  # parts may only be in Drive after a restart
        pj = jobs[p]
        if not Path(pj["video"]).exists() and pj.get("drive"):
            gd.download(pj["drive"]["id"], pj["video"])
        files.append(pj["video"])
    final = OUT_DIR / f"{lid}.mp4"
    lf.concat(files, final)
    durs = [lf.duration(f) for f in files]
    m = r["meta"]
    chap = lf.chapters(m["sections"], durs)
    j.update(status="done", video=str(final), part=None,
             yt={"title": m["title"], "description": (m["description"] + "\n\nChapters:\n" + chap).strip(), "chapters": chap})
    _up.push(lid)


def worker():
    while True:
        jid = jobq.get()
        j = jobs[jid]
        j.update(status="running", started=time.time())
        try:
            if j.get("kind") == "long":
                run_long(jid, j)
            else:
                j["req"]["_user"] = j["user"]
                dest = OUT_DIR / f"{jid}.mp4"
                j.update(status="done", video=str(dest), script=render(jid, j["req"], dest))
                _up.push(jid)
        except Exception as e:  # noqa: BLE001
            msg = redact(e)
            if "quota" in msg.lower() or "429" in msg or "credits" in msg.lower():
                msg = "A free API limit was reached (" + msg[:160] + "). Try again later or pick a free Edge voice."
            j.update(status="error", error=msg[:300])
        finally:
            j["elapsed"] = round(time.time() - j["started"])
            busy = {k[:8] for k, v in jobs.items() if v.get("kind") == "long" and v["status"] in ("queued", "running")}
            pending = {Path(v["video"]).name for v in jobs.values() if v.get("video") and not v.get("drive")} if gd.enabled() else set()
            old = [f for f in sorted(OUT_DIR.glob("*.mp4"), key=os.path.getmtime) if f.name[:8] not in busy and f.name not in pending][:-KEEP_VIDEOS]
            for f in old:
                f.unlink(missing_ok=True)
            for k in [k for k, v in jobs.items() if v.get("status") == "done" and not Path(v["video"]).exists() and not v.get("drive")]:
                jobs.pop(k, None)
            j.pop("req", None)
            save_history()
            jobq.task_done()


for _k in sorted((k for k, v in jobs.items() if v["status"] == "queued"), key=lambda k: jobs[k]["created"]):
    jobq.put(_k)
threading.Thread(target=worker, daemon=True).start()


def keepalive():
    """Render free sleeps after ~15 min without INBOUND traffic, killing renders. While work is queued or
    running, ping our own public URL through the internet every 4 min; idle otherwise (saves free hours)."""
    import urllib.request
    url = (os.environ.get("RENDER_EXTERNAL_URL") or "https://moneyprinter-studio.onrender.com").rstrip("/") + "/healthz"
    while True:
        time.sleep(240)
        if os.environ.get("RENDER") and any(j["status"] in ("queued", "running") for j in list(jobs.values())):
            try:
                urllib.request.urlopen(url, timeout=20).read()
            except Exception as e:  # noqa: BLE001
                print("keepalive failed:", type(e).__name__, flush=True)


threading.Thread(target=keepalive, daemon=True).start()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
PAGE = ((HERE / "index.html").read_text()
        .replace("{{SHELL}}", (HERE / "static/cc-shell.html").read_text())
        .replace("{{IDEAS}}", (HERE / "static/cc-ideas.html").read_text())
        .replace("{{SCENES}}", (HERE / "static/cc-scenes.html").read_text())
        .replace("{{LONG}}", (HERE / "static/cc-long.html").read_text())
        .replace("{{PREVIEW}}", (HERE / "static/cc-preview.html").read_text())
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
        opts += ('<optgroup label="ElevenLabs (free plan ~Eleven v4 · 10k credits/mo &#8776; 10 min audio; commercial use needs attribution)">'
                 + "".join(f'<option value="{v}">{k}</option>' for k, v in ELEVEN_VOICES.items()) + "</optgroup>")
    import json
    fam = json.dumps({n: [f[0], f[2]] for n, f in FONT_META.items()})
    sty = json.dumps({k: {x: y for x, y in v.items() if x in ("text_fore_color", "stroke_color", "stroke_width", "text_background_color")} for k, v in STYLES.items()})
    return HTMLResponse(PAGE.replace("{{VOICES}}", opts).replace("{{USER}}", current_user(req))
                        .replace("{{FONTFAM}}", fam).replace("{{STYLES}}", sty))


@app.post("/login")
def login(req: Request, username: str = Form(...), password: str = Form(...), remember: str = Form("")):
    ip = (req.headers.get("x-forwarded-for") or (req.client.host if req.client else "")).split(",")[0].strip()
    now = time.time()
    f = [t for t in _fails.get(ip, []) if now - t < 900]
    if len(f) >= 8:
        return HTMLResponse(LOGIN.replace("{{ERR}}", "Too many attempts. Wait 15 minutes."), 429)
    p = USERS.get(username.strip()[:40])
    if not p or not hmac.compare_digest(p.encode(), password[:200].encode()):
        _fails[ip] = f + [now]
        time.sleep(1)
        return HTMLResponse(LOGIN.replace("{{ERR}}", "Wrong username or password."), 401)
    _fails.pop(ip, None)
    r = RedirectResponse("/", 303)
    set_session(r, username.strip(), bool(remember))
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/", 303); r.delete_cookie("mps"); return r


# target total length in seconds; legacy keys kept for old clients
LENGTHS = {"15": 15, "30": 30, "60": 60, "90": 90, "120": 120, "180": 180, "short": 15, "medium": 40, "long": 75}


@app.post("/api/generate")
async def generate(req: Request):
    user = current_user(req)
    if not user:
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        b = await req.json()
        assert isinstance(b, dict)
    except Exception:
        return JSONResponse({"error": "Bad request."}, 400)
    topic, script = clean_text(b.get("topic"), 300), clean_text(b.get("script"), 4000)
    if not topic and not script:
        return JSONResponse({"error": "Enter a topic or a script."}, 400)
    if sum(1 for j in jobs.values() if j["status"] in ("queued", "running")) >= 5:
        return JSONResponse({"error": "Queue is full, try again in a few minutes."}, 429)
    if sum(1 for j in jobs.values() if j["status"] in ("queued", "running") and j.get("user") == user) >= 2:
        return JSONResponse({"error": "You already have 2 videos waiting. Let one finish first."}, 429)
    voice = b.get("voice") if b.get("voice") in VOICES.values() else VOICES["Ezinne — Nigerian, female"]
    secs = LENGTHS.get(str(b.get("length")), 15)
    if voice.startswith("elevenlabs:"):
        est = len(script) if script else round(secs * 2.1 * 6.5)
        if est > 2500:
            return JSONResponse({"error": "ElevenLabs scripts are capped at 2,500 characters per video to save credits. Shorten it or use a free Edge voice."}, 400)
        if not use(user, "eleven_chars", est):
            return JSONResponse({"error": f"Daily ElevenLabs limit reached ({CAPS['eleven_chars']} characters per user). Use a free Edge voice or try tomorrow."}, 429)
    if not use(user, "videos"):
        return JSONResponse({"error": f"Daily limit reached ({CAPS['videos']} videos per user). Try again tomorrow."}, 429)
    b["_voice"], b["_secs"] = voice, secs
    jid = str(uuid.uuid4())
    jobs[jid] = dict(status="queued", user=user, req=clean_body(b, 4000), created=time.time(), title=(topic or script)[:90],
                     aspect="16:9" if b.get("aspect") == "16:9" else "9:16",
                     voice=next((k.split(" —")[0] for k, v in VOICES.items() if v == voice), ""))
    save_history()
    jobq.put(jid)
    return {"id": jid}


def make_params(b):
    topic, script = clean_text(b.get("topic"), 300), clean_text(b.get("script"), 4000)
    voice, secs = b["_voice"], b["_secs"]
    sp = sub_params(b)
    loc = [p for p in (b.get("_local") or []) if isinstance(p, str)]
    if loc:  # scene editor: user-ordered clips/images, one per scene, Ken Burns on images
        words = max(len(script.split()), 1)
        clip = max(2, min(10, round(words / 2.4 / len(loc))))
        sp = dict(sp, video_source="local", video_concat_mode="sequential", video_clip_duration=clip,
                  video_materials=[MaterialInfo(provider="local", url=p) for p in loc])
    return VideoParams(
        video_subject=topic or script[:80], video_script=script,
        video_terms=[clean_text(t, 40) for t in str(b.get("terms") or "")[:400].split(",") if t.strip()][:6] or None,
        **{k: v for k, v in dict(video_clip_duration=num(b.get("clip"), 4, 2, 10, int), video_source="pixabay").items() if k not in sp},
        video_count=1, video_language="en", voice_name=voice, voice_rate=num(b.get("rate"), 1.0, 0.8, 1.2),
        bgm_type="random" if b.get("music", True) else "", bgm_volume=0.15,
        n_threads=1, paragraph_number=1 if secs <= 30 else 2 if secs <= 60 else 3 if secs <= 90 else 4 if secs <= 120 else 6,
        video_script_prompt=f"Write in English. Keep the whole script to {round(secs * 2.1 * 0.9)} to {round(secs * 2.1 * 1.1)} words in total (about {secs} seconds spoken).",
        **sp)


def sub_params(b):
    """Subtitle/look settings from a request body, all validated (shared by renders and previews)."""
    hexc = lambda v, d: v if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v) else d
    if b.get("sub_style") == "custom":
        style = dict(text_fore_color=hexc(b.get("sub_color"), "#FFFFFF"), stroke_color=hexc(b.get("sub_stroke"), "#000000"),
                     stroke_width=num(b.get("sub_stroke_w"), 1.5, 0.0, 8.0),
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
                 subtitle_uppercase=bool(b.get("sub_upper")),
                 subtitle_bold=bool(b.get("sub_bold")), subtitle_italic=bool(b.get("sub_italic")))
    return dict(video_aspect=VideoAspect.landscape if b.get("aspect") == "16:9" else VideoAspect.portrait,
                subtitle_enabled=bool(b.get("subtitles", True)), font_name=font,
                font_size=num(b.get("font_size"), 48, 16, 120, int), subtitle_position=pos,
                custom_position=num(b.get("sub_custom"), 70.0, 0.0, 100.0), subtitle_display_mode=mode,
                **style, **extra)


sys.path.insert(0, str(HERE))
import ideas as idealab  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402


@app.post("/api/research")
async def api_research(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    u = current_user(req)
    t = clean_body(await req.json()).get("topic") or ""
    t = t.strip()[:120]
    if not t:
        return JSONResponse({"error": "Enter a niche or topic."}, 400)
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily idea limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    return {"sources": await run_in_threadpool(idealab.research, t)}


@app.post("/api/ideas")
async def api_ideas(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json())
    if not isinstance(b, dict) or not str(b.get("topic") or "").strip():
        return JSONResponse({"error": "Enter a niche or topic."}, 400)
    if not use(current_user(req), "ai"):
        return JSONResponse({"error": f"Daily idea limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    try:
        res = b.get("research") or await run_in_threadpool(idealab.research, b["topic"])
        return {"ideas": await run_in_threadpool(idealab.ideas, b, res)}
    except Exception as e:
        return JSONResponse({"error": "Idea generation failed: " + redact(e)[:120]}, 502)


@app.post("/api/script")
async def api_script(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json())
    if not isinstance(b, dict):
        return JSONResponse({"error": "Bad request."}, 400)
    if not use(current_user(req), "ai"):
        return JSONResponse({"error": f"Daily idea limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    try:
        return await run_in_threadpool(idealab.script, b)
    except Exception as e:
        return JSONResponse({"error": "Script writing failed: " + redact(e)[:120]}, 502)


from fastapi import UploadFile, File  # noqa: E402


@app.post("/api/scenes")
async def api_scenes(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json(), 4000)
    if b.get("q"):  # re-search a single scene
        return {"options": await run_in_threadpool(scn.pixabay, clean_text(b["q"], 60), b.get("aspect") or "9:16")}
    script = clean_text(b.get("script"), 4000)
    if len(script.split()) < 5:
        return JSONResponse({"error": "Write or generate a script first (scene matching needs the script)."}, 400)
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily AI limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    return {"scenes": await run_in_threadpool(scn.plan, script, b.get("aspect") or "9:16")}


@app.post("/api/upload")
async def api_upload(req: Request, file: UploadFile = File(...)):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        return await run_in_threadpool(scn.save_upload, u, file.filename, file.file)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)


@app.get("/uploads/{uid}")
def upload_thumb(uid: str, req: Request):
    u = current_user(req)
    base = uid.replace(".thumb.jpg", "")
    p = scn.upload_path(u or "", base) if u else None
    if not p:
        return JSONResponse({"error": "not found"}, 404)
    t = p.with_name(p.stem + ".thumb.jpg")
    return FileResponse(t if uid.endswith(".thumb.jpg") and t.exists() else p)


@app.post("/api/hook")
async def api_hook(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    script = clean_text(clean_body(await req.json(), 4000).get("script"), 4000)
    if len(script.split()) < 4:
        return JSONResponse({"error": "Write or generate a script first."}, 400)
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily AI limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    try:
        return await run_in_threadpool(scn.hook, script)
    except Exception as e:
        return JSONResponse({"error": "Hook check failed: " + redact(e)[:120]}, 502)


@app.post("/api/long")
async def api_long(req: Request):
    import longform as lf
    user = current_user(req)
    if not user:
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        b = await req.json(); assert isinstance(b, dict)
    except Exception:
        return JSONResponse({"error": "Bad request."}, 400)
    script = clean_text(b.get("script"), 30000)
    words = len(script.split())
    if words < 150:
        return JSONResponse({"error": "Long Script Mode needs at least ~150 words. Use the normal mode for short videos."}, 400)
    parts = lf.split_parts(script)
    if b.get("dry"):
        return {"parts": [{"words": len(p.split()), "start": p[:90]} for p in parts], "minutes": round(words / lf.WPM, 1)}
    if any(v.get("user") == user and v.get("kind") == "long" and v["status"] in ("queued", "running") for v in jobs.values()):
        return JSONResponse({"error": "You already have a long video rendering. Let it finish first."}, 429)
    voice = b.get("voice") if b.get("voice") in VOICES.values() else VOICES["Ezinne — Nigerian, female"]
    if voice.startswith("elevenlabs:"):
        if not use(user, "eleven_chars", len(script)):
            return JSONResponse({"error": f"This script is {len(script):,} characters, over your ElevenLabs allowance ({CAPS['eleven_chars']:,}/day). Use a free Edge voice for long-form."}, 429)
    mins = max(1, round(words / lf.WPM))
    if not use(user, "minutes", mins):
        return JSONResponse({"error": f"Daily long-form limit is {CAPS['minutes']} minutes of video. This script is ~{mins} min. Try a shorter script or tomorrow."}, 429)
    title = clean_text(b.get("title"), 90) or script[:60]
    st = clean_body({k: v for k, v in b.items() if k not in ("script", "parts", "done", "meta", "used", "scenes", "_local")}, 300)
    st.update(_voice=voice, _secs=180, aspect="16:9" if b.get("aspect", "16:9") == "16:9" else "9:16", parts=parts)
    lid = str(uuid.uuid4())
    jobs[lid] = dict(status="queued", kind="long", user=user, req=st, created=time.time(), title=title, aspect=st["aspect"],
                     part=f"{len(parts)} parts queued", voice=next((k.split(" —")[0] for k, v in VOICES.items() if v == voice), ""))
    save_history(); jobq.put(lid)
    return {"id": lid, "parts": len(parts)}


@app.get("/api/status/{jid}")
def status(jid: str, req: Request):
    j = jobs.get(jid)
    if not j or current_user(req) != j.get("user"):
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
        d = {m: j.get(m) for m in ("status", "title", "aspect", "voice", "elapsed", "error", "part", "yt", "kind")}
        d["drive"] = bool(j.get("drive"))
        d["id"] = k
        d.update({x: y for x, y in status(k, req).items() if x in ("progress", "elapsed", "ahead")} if j["status"] in ("queued", "running") else {})
        out.append(d)
    return out[:30]


@app.get("/video/{jid}")
def video(jid: str, req: Request, dl: int = 0):
    j = jobs.get(jid)
    if not j or current_user(req) != j.get("user") or j.get("status") != "done":
        return JSONResponse({"error": "not found"}, 404)
    fn = f"moneyprinter-{jid[:8]}.mp4" if dl else None
    if Path(j["video"]).exists():
        return FileResponse(j["video"], media_type="video/mp4", filename=fn)
    if j.get("drive") and gd.enabled():  # stream from Drive (supports seeking via Range)
        from fastapi.responses import StreamingResponse
        try:
            r = gd.open_media(j["drive"]["id"], req.headers.get("range"))
        except Exception:
            return JSONResponse({"error": "Drive unavailable"}, 502)
        h = {k: r.headers[k] for k in ("Content-Length", "Content-Range") if r.headers.get(k)}
        h["Accept-Ranges"] = "bytes"
        if fn:
            h["Content-Disposition"] = f'attachment; filename="{fn}"'

        def gen():
            with r:
                while b := r.read(256 * 1024):
                    yield b
        return StreamingResponse(gen(), status_code=r.status, media_type="video/mp4", headers=h)
    return JSONResponse({"error": "not found"}, 404)


@app.get("/thumb/{jid}")
def thumb(jid: str, req: Request):
    j = jobs.get(jid)
    if not j or current_user(req) != j.get("user") or not (j.get("drive") or {}).get("thumb") or not gd.enabled():
        return JSONResponse({"error": "not found"}, 404)
    with gd.open_media(j["drive"]["thumb"]) as r:
        return HTMLResponse(r.read(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


ADMINS = {u.strip() for u in os.environ.get("ADMIN_USERS", "miracle").split(",") if u.strip()}


@app.get("/admin/drive", response_class=HTMLResponse)
def admin_drive(req: Request, saved: str = ""):
    if current_user(req) not in ADMINS:
        return HTMLResponse("Not allowed", 403)
    st = ("✅ Connected — finished videos, thumbnails and History are saved to Drive folder “" + gd.folder_name() + "”." if gd.enabled()
          else "⚪ Not connected." if gd.configured() else
          "⚪ Disabled: set GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET on Render first (see setup steps).")
    btn = '<p><a href="/oauth/google/start" style="background:#a855f7;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none">Connect Google Drive</a></p>' if gd.configured() else ""
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="font-family:sans-serif;background:#0b0a1a;color:#e9e6ff;max-width:640px;margin:40px auto;padding:0 16px">
<h2>Google Drive storage</h2><p>{st}</p>{saved and '<p>'+saved+'</p>'}{btn}<p style="color:#9c97c0;font-size:13px">Scope: drive.file (the app can only see files it created). Redirect URI: {gd.redirect_uri()}</p><p><a href="/" style="color:#a855f7">← Back</a></p>""")


@app.get("/oauth/google/start")
def oauth_start(req: Request):
    if current_user(req) not in ADMINS or not gd.configured():
        return HTMLResponse("Not allowed", 403)
    st = secrets.token_urlsafe(24)
    r = RedirectResponse(gd.auth_url(st), 303)
    r.set_cookie("gst", st, max_age=600, httponly=True, samesite="lax", secure=os.environ.get("RENDER") is not None)
    return r


@app.get("/oauth/google/callback", response_class=HTMLResponse)
def oauth_cb(req: Request, code: str = "", state: str = "", error: str = ""):
    if current_user(req) not in ADMINS or not gd.configured():
        return HTMLResponse("Not allowed", 403)
    if error or not code or not state or not hmac.compare_digest(state, req.cookies.get("gst", "")):
        return admin_drive(req, saved="❌ Google sign-in was cancelled or expired. Try again.")
    try:
        rt = gd.exchange(code)
    except Exception as e:  # noqa: BLE001
        return admin_drive(req, saved="❌ Token exchange failed: " + redact(e)[:100])
    for k, v in jobs.items():
        if v.get("status") == "done" and not v.get("drive"):
            _up.push(k)
    _up.history_changed()
    note = ("✅ Connected for this server session. To keep it after restarts, copy this refresh token into Render → Environment as "
            "<b>GDRIVE_REFRESH_TOKEN</b> (shown once, keep it private):<br><textarea readonly style='width:100%;height:70px'>"
            + (rt or "(Google did not return a new refresh token — remove the app at myaccount.google.com/permissions and connect again)")
            + "</textarea>")
    r = admin_drive(req, saved=note); r.delete_cookie("gst"); r.headers["Cache-Control"] = "no-store"
    return r


FONT_DIR = ROOT / "resource" / "fonts"


def _font_meta():
    from PIL import ImageFont
    out = {}
    for n in sorted(FONTS):
        try:
            f = ImageFont.truetype(str(FONT_DIR / n), 1000)
            fam, sub = f.getname()
            a, d = f.getmetrics()
        except Exception:
            continue
        out[n] = (fam, sub or "", round(1000 / max(1, a + d), 3))
    return out


FONT_META = _font_meta()
FONT_CSS = "".join(
    f"@font-face{{font-family:'S {fam}';src:url('/fonts/{n}');font-weight:{700 if 'Bold' in sub else 400};"
    f"font-style:{'italic' if 'Italic' in sub else 'normal'};font-display:swap}}"
    for n, (fam, sub, _r) in FONT_META.items() if n.endswith(".ttf"))


@app.get("/fonts.css")
def fonts_css(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    from fastapi.responses import Response
    return Response(FONT_CSS, media_type="text/css", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/fonts/{name}")
def font_file(name: str, req: Request):
    if not current_user(req) or name not in FONTS or not name.endswith(".ttf"):
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(FONT_DIR / name, media_type="font/ttf", headers={"Cache-Control": "private, max-age=86400"})


_prev_lock = threading.Lock()


@app.post("/api/preview-frame")
async def preview_frame(req: Request):
    """Render ONE real subtitle frame with ffmpeg/libass on a dark background. No TTS, no footage, no credits."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        b = clean_body(await req.json(), 400)
        assert isinstance(b, dict)
    except Exception:
        return JSONResponse({"error": "Bad request."}, 400)
    if not use(u, "previews"):
        return JSONResponse({"error": "Daily preview limit reached."}, 429)
    if any(j["status"] == "running" for j in jobs.values()) and os.environ.get("RENDER"):
        return JSONResponse({"error": "A video is rendering right now (512MB server). Try the preview again when it finishes; the live box above is exact enough meanwhile."}, 409)
    if not _prev_lock.acquire(blocking=False):
        return JSONResponse({"error": "A preview is already rendering."}, 429)
    try:
        from app.services import video as vs
        sp = sub_params(b)
        sp["subtitle_enabled"] = True
        params = VideoParams(video_subject="preview", **sp)
        text = clean_text(b.get("text"), 200) or "This is how your subtitles will look"
        if params.subtitle_uppercase:
            text = text.upper()
        w, h = (960, 540) if b.get("aspect") == "16:9" else (540, 960)
        # the real render is 1080 wide/high; scale font so proportions match
        params.font_size = max(8, round(params.font_size / 2))
        params.stroke_width = (params.stroke_width or 0) / 2
        import tempfile
        out = os.path.join(tempfile.gettempdir(), f"prev_{uuid.uuid4().hex}.png")
        ok = await run_in_threadpool(vs.render_preview_frame, params, text, w, h, out, num(b.get("at"), None, 0, 4) if b.get("at") is not None else None)
        if not ok:
            return JSONResponse({"error": "Preview render failed."}, 500)
        data = Path(out).read_bytes(); Path(out).unlink(missing_ok=True)
        from fastapi.responses import Response
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})
    finally:
        _prev_lock.release()


@app.middleware("http")
async def security_headers(req: Request, call_next):
    r = await call_next(req)
    s = _session(req)
    if s and s[2] and s[1] - time.time() < (SESSION_DAYS - 1) * 86400 and "set-cookie" not in r.headers:
        set_session(r, s[0], True)  # sliding renewal, at most once a day
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "same-origin"
    return r


# leftovers from a crash or restart mid-render
shutil.rmtree(ROOT / "storage" / "tasks", ignore_errors=True)
shutil.rmtree(ROOT / "storage" / "cache_videos", ignore_errors=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
