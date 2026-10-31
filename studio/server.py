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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from app.models import const  # noqa: E402
from app.models.schema import VideoParams, VideoAspect, MaterialInfo  # noqa: E402
from app.services import state as sm, task as tm, bgm as bgm_service  # noqa: E402
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
                        "SESSION_SECRET", "PEXELS_API_KEY", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN", "TOKEN_ENC_KEY") if len(v := os.environ.get(k, "")) >= 6]
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
        "minutes": int(os.environ.get("CAP_MINUTES", 30)),
        "flow": int(os.environ.get("CAP_FLOW", 50))}  # Flow files accepted per user per day (helper paces 75-120 s)
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
        return {str(k)[:40]: clean_body(v, n) for k, v in list(b.items())[:90]}
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
import urllib.parse  # noqa: E402


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
if gd.enabled():  # Render's disk is wiped on restart: rebuild History (and the encrypted token vault) from Drive
    try:
        _load((gd.admin().get_json("history.json") or {}).items())
        print("history restored from admin Drive:", len(jobs), "items", flush=True)
    except Exception as e:  # noqa: BLE001
        print("drive history load failed:", type(e).__name__, flush=True)
try:
    print("drive accounts connected:", gd.load_vault(), flush=True)
except Exception as e:  # noqa: BLE001
    print("token vault load failed:", type(e).__name__, flush=True)
for _u in list(gd._users):
    try:
        _load(((k, v) for k, v in (gd.account_for(_u).get_json("history.json") or {}).items() if isinstance(v, dict) and v.get("user") == _u))
    except Exception as e:  # noqa: BLE001
        print("user drive history load failed:", type(e).__name__, flush=True)


def save_history():
    import json
    done = {k: {m: v.get(m) for m in META} for k, v in list(jobs.items())}
    HIST.write_text(json.dumps(done))
    if "_up" in globals():
        _up.history_changed()


def _snapshot(key):
    """history.json for one Drive: a connected user's Drive holds only their own items; the admin Drive holds the rest."""
    out = {}
    for k, v in list(jobs.items()):
        acct = (v.get("drive") or {}).get("acct") or gd.ADMIN
        own = acct if not acct.startswith("~") else gd.ADMIN
        if key == gd.ADMIN and own != gd.ADMIN:
            continue
        if key != gd.ADMIN and (v.get("user") != key or own != key):
            continue
        out[k] = {m: v.get(m) for m in META}
    return out


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
        if req.get("smart", True) and not req.get("scenes") and not req.get("_local"):
            import director
            try:
                def _prog(n, _j=jid):
                    if _j in jobs:
                        jobs[_j]["dprog"] = max(int(n), int(jobs[_j].get("dprog") or 0))

                def _log(m, _j=jid):
                    print(m, flush=True)
                    if _j in jobs and m.startswith("director: Using backup"):
                        jobs[_j]["notice"] = "Using backup images: " + m[23:]
                    if _j in jobs and m.startswith("director: "):
                        st = m[10:]
                        jobs[_j]["stage"] = ("Writing & critiquing the script" if st.startswith("writing") else
                                             "Planning shots" if st.startswith("planning") else
                                             "Choosing footage" if st.startswith("scored") else
                                             "Painting AI visuals" if st.startswith("AI images") else
                                             "Waiting for Google Flow clips" if st.startswith("sending scenes") else
                                             "Cutting clips" if st.startswith("cutting") else
                                             "Adding crossfades" if st.startswith("joined") else jobs[_j].get("stage", ""))
                tmpl = TEMPLATES.get(req.get("template") or "", "") or clean_text(req.get("style"), 600)
                d = director.direct(jid, clean_text(req.get("topic"), 300), clean_text(req.get("script"), 4000), req["_secs"],
                                    req["_voice"], num(req.get("rate"), 1.0, 0.8, 1.2), req.get("aspect") or "9:16",
                                    utils.storage_dir("local_videos", create=True), log=_log, progress=_prog,
                                    crossfade=req.get("crossfade", True) is not False, style=tmpl,
                                    visuals=ai_allowed(req.get("_user", ""), req),
                                    ai_style=aiv_norm(req.get("ai_style")),
                                    ai_prompts=[clean_text(x, 300) for x in (req.get("ai_prompts") or [])][:40] or None,
                                    keep_script=bool(req.get("keep_script")), chars=chars_for(req), preset=preset_of(req),
                                    free_ai=is_admin(req.get("_user", "")),
                                    flow=flow_runner(req.get("_user", ""), jid, req) if flow_engine(req.get("_user", ""), req) else None)
                if jid in jobs:
                    jobs[jid]["dprog"], jobs[jid]["stage"] = 30, "Voice, subtitles & final render"
                    jobs[jid]["visuals"] = d.get("providers") or []
                if len(d["clips"]) >= 1:
                    req.update(script=d["script"], _local=d["clips"], _clipmax=d["clip_max"])
                    req["_director"] = {k: d[k] for k in ("look", "report", "critique", "hook", "draft")}
                    if jid in jobs:
                        jobs[jid]["script"] = d["script"]
            except Exception as e:
                print("director failed, using classic pipeline:", redact(str(e))[:200], flush=True)
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
        preq.update(script=parts[n], topic="", _user=j["user"], _long=True, keep_script=True)
        pm = r.get("part_music") if isinstance(r.get("part_music"), list) else []
        if n < len(pm) and pm[n]:  # per-part / per-chapter track ("none" = silence for this part)
            if pm[n] == "none":
                preq["music"] = False
            else:
                preq["music_track"], preq["music"] = str(pm[n])[:120], True
        # per-sentence footage, never reusing a clip from an earlier part
        plan = scn.plan(parts[n], r.get("aspect") or "16:9") if r.get("visuals", "stock") == "stock" and not r.get("smart") else []
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
            gd.account_by_key(pj["drive"].get("acct")).download(pj["drive"]["id"], pj["video"])
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
            pending = {Path(v["video"]).name for v in jobs.values() if v.get("video") and not v.get("drive")} if gd.configured() else set()
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
        .replace("{{SUBADV}}", (HERE / "static/cc-subadv.html").read_text())
        .replace("{{CHARS}}", (HERE / "static/cc-chars.html").read_text() + (HERE / "static/cc-presets.html").read_text())
        .replace("{{HEADER}}", (HERE / "static/cc-header.html").read_text())
        .replace("{{APP}}", "MoneyPrinter Studio"))
LOGIN = (Path(__file__).parent / "login.html").read_text()
_ix = (HERE / "index.html").read_text()
SUBS = (_ix[_ix.index('<div class=cc-field style="grid-column:1/-1"><label>Subtitle preset'):_ix.index('<div class=cc-field><label>Clip duration')]
        .replace("{{PREVIEW}}", (HERE / "static/cc-preview.html").read_text()))
SUBS += "<script>" + _ix[_ix.index("const PRESETS="):_ix.index("</script>", _ix.index("const PRESETS="))].replace("$(k)", "document.getElementById(k)") + "</script>"
SUBS += _ix[_ix.index("<script>(()=>{const g=i=>document.getElementById(i);['sub_bold'"):].split("</script>")[0] + "</script>"
LONGP = ((HERE / "longform.html").read_text().replace("{{SUBS}}", SUBS)
         .replace("{{SUBADV}}", (HERE / "static/cc-subadv.html").read_text())
         .replace("{{CHARS}}", (HERE / "static/cc-chars.html").read_text() + (HERE / "static/cc-presets.html").read_text()).replace("{{SHELL}}", (HERE / "static/cc-shell.html").read_text())
         .replace("{{HEADER}}", (HERE / "static/cc-header.html").read_text()).replace("{{APP}}", "MoneyPrinter Studio"))
LOCKED = "<h2 style='font-family:sans-serif;text-align:center;margin-top:20vh'>🔒 MoneyPrinter Studio is locked.<br><small>The owner must set APP_USERS.</small></h2>"


def is_admin(u):
    return u in ADMINS


def ai_gate(u):
    """AI visuals: the admin may use the free image models; everyone else only through their OWN paired Google Flow."""
    if is_admin(u) or u in _keys():
        return None
    return "Connect your Google Flow to use AI visuals (Account → Connect my Google Flow). Stock footage and your own uploads still work."


def ai_allowed(u, req):
    want = req.get("visuals", "ai") == "ai"
    return "ai" if want and not ai_gate(u) else "stock"


def aiv_norm(v):
    import stylepresets as sp
    return v if v in ("realistic", "pixar", "stick") or sp.get(v) else "realistic"


def preset_of(req):
    import stylepresets as sp
    v = req.get("style_preset")
    if v is None and req.get("_long"):
        v = sp.DEFAULT_LONG  # long-form defaults to the Ancient explainer (Ink) look
    return v if sp.get(v) else None


@app.get("/api/presets")
def api_presets():
    import stylepresets as sp
    return {"presets": sp.public(), "default_long": sp.DEFAULT_LONG}


@app.get("/healthz")
def health():
    return {"ok": True}


def voice_opts():
    opts = '<optgroup label="Free voices (Edge)">' + "".join(f'<option value="{v}">{k}</option>' for k, v in EDGE_VOICES.items()) + "</optgroup>"
    if os.environ.get("ELEVENLABS_API_KEY"):
        opts += '<optgroup label="ElevenLabs (uses credits)">' + "".join(f'<option value="{v}">{k}</option>' for k, v in ELEVEN_VOICES.items()) + "</optgroup>"
    return opts


HOMEP = ((HERE / "home.html").read_text().replace("{{SHELL}}", (HERE / "static/cc-shell.html").read_text())
         .replace("{{HEADER}}", (HERE / "static/cc-header.html").read_text()).replace("{{APP}}", "MoneyPrinter Studio"))


@app.get("/", response_class=HTMLResponse)
def home(req: Request):
    if not USERS:
        return HTMLResponse(LOCKED, 503)
    u = current_user(req)
    if not u:
        return HTMLResponse(LOGIN.replace("{{ERR}}", ""))
    import html as _h
    return HTMLResponse(HOMEP.replace("{{USER}}", _h.escape(u)))


@app.get("/api/usage")
def api_usage(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return {"caps": {k: v for k, v in CAPS.items() if k != "previews"},
            "used": {k: _usage.get((u, k, day), 0) for k in CAPS if k != "previews"}}


@app.get("/longform", response_class=HTMLResponse)
def longform_page(req: Request):
    if not current_user(req):
        return RedirectResponse("/", 303)
    import json
    fam = json.dumps({n: [f[0], f[2]] for n, f in FONT_META.items()})
    sty = json.dumps({k: {x: y for x, y in v.items() if x in ("text_fore_color", "stroke_color", "stroke_width", "text_background_color")} for k, v in STYLES.items()})
    return HTMLResponse(LONGP.replace("{{VOICES}}", voice_opts()).replace("{{USER}}", current_user(req))
                        .replace("{{FONTFAM}}", fam).replace("{{STYLES}}", sty))


@app.get("/shorts", response_class=HTMLResponse)
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
        return HTMLResponse(LOGIN.replace("{{ERR}}", "Too many tries. For safety, sign-in is paused for 15 minutes on this connection. Please try again later."), 429)
    p = USERS.get(username.strip()[:40])
    if not p or not hmac.compare_digest(p.encode(), password[:200].encode()):
        _fails[ip] = f + [now]
        time.sleep(1)
        return HTMLResponse(LOGIN.replace("{{ERR}}", "That username and password don\u2019t match. Check for typos or Caps Lock and try again."), 401)
    _fails.pop(ip, None)
    r = RedirectResponse("/", 303)
    set_session(r, username.strip(), bool(remember))
    return r


@app.get("/login", response_class=HTMLResponse)
def login_page(req: Request):
    return RedirectResponse("/", 303) if current_user(req) else HTMLResponse(LOGIN.replace("{{ERR}}", ""))


@app.get("/logout")
def logout():
    r = RedirectResponse("/", 303); r.delete_cookie("mps"); return r


TEMPLATES = {
    "top5": "Top 5 countdown. Hook line first, then exactly five numbered items counting down from 5 to 1, each one short punchy fact "
            "with a concrete detail, the last (#1) the most surprising. End with a one-line question to drive comments.",
    "story": "Story time, first person, present tense. Open mid-action with tension, build with 2-3 vivid beats, a twist or reveal near the end, "
             "and a short reflective closing line. Conversational, like telling a friend.",
    "product": "Product ad. Hook with the pain point, introduce the product as the fix, show 3 concrete benefits (not features), "
               "one line of social proof, and a clear call to action at the end.",
}

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
    if b.get("visuals") == "ai" and ai_gate(user):
        return JSONResponse({"error": ai_gate(user), "account": "/account"}, 403)
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


def music_file(b):
    """'' = random library track; 'lib:NAME' = a built-in track; 'up:NAME' = this user's upload."""
    m = str(b.get("music_track") or "")
    try:
        if m.startswith("lib:"):
            return bgm_service.resolve_builtin_bgm_file(os.path.basename(m[4:]))
        if m.startswith("up:"):
            f = Path(bgm_service.uploaded_bgm_dir()) / os.path.basename(m[3:])
            if not f.name.startswith(_ufx(b.get("_user", ""))):
                return ""
            return str(f) if f.exists() else _music_from_drive(b.get("_user", ""), f.name)
    except Exception:
        return ""
    return ""


def _ufx(u):
    return "u_" + hashlib.sha1(str(u).encode()).hexdigest()[:8] + "_"


def make_params(b):
    topic, script = clean_text(b.get("topic"), 300), clean_text(b.get("script"), 4000)
    voice, secs = b["_voice"], b["_secs"]
    sp = sub_params(b)
    loc = [p for p in (b.get("_local") or []) if isinstance(p, str)]
    if loc:  # scene editor: user-ordered clips/images, one per scene, Ken Burns on images
        words = max(len(script.split()), 1)
        clip = int(b["_clipmax"]) if b.get("_clipmax") else max(2, min(10, round(words / 2.4 / len(loc))))
        sp = dict(sp, video_source="local", video_concat_mode="sequential", video_clip_duration=clip,
                  video_materials=[MaterialInfo(provider="local", url=p) for p in loc])
    return VideoParams(
        video_subject=topic or script[:80], video_script=script,
        video_terms=[clean_text(t, 40) for t in str(b.get("terms") or "")[:400].split(",") if t.strip()][:6] or None,
        **{k: v for k, v in dict(video_clip_duration=num(b.get("clip"), 4, 2, 10, int), video_source="pixabay").items() if k not in sp},
        video_count=1, video_language="en", voice_name=voice, voice_rate=num(b.get("rate"), 1.0, 0.8, 1.2),
        bgm_type="random" if b.get("music", True) else "", bgm_file=music_file(b), bgm_volume=(num(b.get("music_vol"), 30, 0, 100, int) / 100 * (1.1 if b.get("duck", True) is not False else 0.55)) if b.get("music_vol") is not None else (0.32 if b.get("duck", True) is not False else 0.15),
        music_fade_in=num(b.get("music_fade_in"), 1.5, 0.0, 8.0), music_fade_out=num(b.get("music_fade_out"), 2.5, 0.0, 10.0),
        duck_music=b.get("duck", True) is not False,
        watermark_file=str(Path(__file__).parent / "static" / "wm.png") if b.get("watermark") else "",
        end_card_text=("@" + re.sub(r"[^A-Za-z0-9_.]", "", str(b.get("end_handle") or "calmcrazechronicles"))[:30]) if b.get("end_card") else "",
        output_hd=b.get("quality") == "1080",
        n_threads=1, paragraph_number=1 if secs <= 30 else 2 if secs <= 60 else 3 if secs <= 90 else 4 if secs <= 120 else 6,
        video_script_prompt=(TEMPLATES.get(b.get("template") or "", "") + " ") * bool(TEMPLATES.get(b.get("template") or "")) + f"Write in English. Keep the whole script to {round(secs * 2.1 * 0.9)} to {round(secs * 2.1 * 1.1)} words in total (about {secs} seconds spoken).",
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
    adv = dict(sub_shadow=num(b.get("sub_shadow"), 0.0, 0.0, 8.0), sub_bg_opacity=num(b.get("sub_bg_opacity"), 67, 0, 100, int),
               sub_pad=num(b.get("sub_pad"), 10, 0, 40, int), sub_spacing=num(b.get("sub_spacing"), 0.0, -5.0, 20.0),
               words_per_line=num(b.get("words_per_line"), 0, 0, 12, int), max_lines=num(b.get("max_lines"), 2, 1, 3, int),
               sub_anim=b.get("sub_anim") if b.get("sub_anim") in ("pop", "fade", "bounce") else "",
               sub_margin=num(b.get("sub_margin"), 8, 0, 30, int))
    if not preset and (adv["words_per_line"] or adv["sub_anim"] or adv["sub_shadow"] or adv["sub_spacing"] or b.get("sub_adv")):
        preset = "plain"  # advanced options need the ASS renderer
    if preset:
        mode = "word_by_word"  # presets regroup word timings into ASS lines
    extra = dict(subtitle_preset=preset, highlight_color=hexc(b.get("sub_hl"), "#A855F7"),
                 subtitle_uppercase=bool(b.get("sub_upper")),
                 subtitle_bold=bool(b.get("sub_bold")), subtitle_italic=bool(b.get("sub_italic")), **adv)
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


@app.post("/api/scene/ai")  # alias: older docs/tests used this path
@app.post("/api/ai_image")
async def api_ai_image(req: Request):
    """Scene editor: (re)generate one AI image for a scene and store it as the user's upload."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json(), 1400)
    text, prompt = clean_text(b.get("text"), 400), clean_text(b.get("prompt"), 1400)
    if not text and not prompt:
        return JSONResponse({"error": "Nothing to draw."}, 400)
    if not is_admin(u):  # free image models are admin-only (checked before the quota so a refusal costs nothing)
        return JSONResponse({"error": "Free AI image models are for the studio owner only. Connect your Google Flow and use “Send to Flow” for AI scenes.", "account": "/account"}, 403)
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily AI limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    import aivisuals as aiv, director, io
    style = aiv_norm(b.get("style"))
    W, H = (1024, 576) if b.get("aspect") == "16:9" else (576, 1024)
    seed = int(num(b.get("seed"), 0, 0, 999999, int)) or secrets.randbelow(999999)

    def work():
        p = prompt
        if not p:
            p = aiv.prompts([text], clean_text(b.get("topic"), 200) or text, style, director.gem, aspect=b.get("aspect") or "9:16")[0][0]
        elif "Story bible" not in p:
            p = f"{p}. {aiv.STYLES[style]}"
        path, prov, p, score, tries = aiv.generate_verified(p, text or p[:200], W, H, seed, director.gem)
        up = scn.save_upload(u, "ai-scene.jpg", io.BytesIO(Path(path).read_bytes()))
        return dict(up, prompt=p, provider=prov, seed=seed, score=score, tries=len(tries))
    try:
        return await run_in_threadpool(work)
    except Exception as e:
        return JSONResponse({"error": "The free image models are busy right now. Try again in a minute. (" + redact(str(e))[:80] + ")"}, 503)


@app.get("/api/features")
def api_features(req: Request):
    if not current_user(req):
        return JSONResponse({"error": "not logged in"}, 401)
    import aivisuals as aiv
    return {"pexels": bool(os.environ.get("PEXELS_API_KEY")), "pixabay": bool(os.environ.get("PIXABAY_API_KEY")),
            "commons": True, "nasa": True, "ai": aiv.status(), "templates": list(TEMPLATES),
            "free_models": is_admin(current_user(req)), "ai_gate": ai_gate(current_user(req)), "flow_connected": current_user(req) in _keys()}


@app.get("/api/music")
def api_music(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    lib = [{"id": "lib:" + os.path.basename(f), "name": f"Library track {i + 1}"} for i, f in enumerate(sorted(bgm_service.list_builtin_bgm_files()))]
    pre = _ufx(u)
    d = Path(bgm_service.uploaded_bgm_dir())
    mine = [{"id": "up:" + f.name, "name": f.name[len(pre):]} for f in sorted(d.glob(pre + "*")) if f.is_file()]
    return {"library": lib, "mine": mine}


@app.get("/music/{mid}")
def music_file_get(mid: str, req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    f = music_file({"music_track": mid, "_user": u})
    return FileResponse(f, media_type="audio/mpeg") if f else JSONResponse({"error": "not found"}, 404)


@app.post("/api/music/upload")
async def api_music_upload(req: Request, file: UploadFile = File(...)):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    pre = _ufx(u)
    d = Path(bgm_service.uploaded_bgm_dir())
    if len(list(d.glob(pre + "*"))) >= 10:
        return JSONResponse({"error": "You can keep up to 10 tracks. Delete one first."}, 400)
    name = pre + re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(file.filename or "track.mp3"))[-60:]
    try:
        saved = await run_in_threadpool(bgm_service.save_bgm_upload, name, file.file)
    except Exception as e:
        return JSONResponse({"error": "Upload an mp3, m4a, wav or ogg under 20 MB. (" + str(e)[:80] + ")"}, 400)
    final = d / (name.rsplit(".", 1)[0][:70] + "_" + uuid.uuid4().hex[:4] + Path(saved).suffix)
    os.replace(saved, final)
    _music_to_drive(u, final)  # personal library survives free-plan restarts
    return {"id": "up:" + final.name, "name": final.name[len(pre):], "drive": bool(gd.target_for(u)[1])}


@app.post("/api/captions/{jid}")
async def api_captions(jid: str, req: Request):
    u = current_user(req)
    j = jobs.get(jid)
    if not u or not j or j.get("user") != u:
        return JSONResponse({"error": "not found"}, 404)
    if j.get("captions"):
        return j["captions"]
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily AI limit reached ({CAPS['ai']} requests). Try again tomorrow."}, 429)
    import director
    src = clean_text(j.get("script") or j.get("title"), 3000)

    def work():
        d = director.gem(f"""Write publishing copy for this short video, per platform. Script:
{src}
Return JSON {{"tiktok": {{"caption": "max 150 chars, hooky, 1-2 emojis", "hashtags": ["#..", 5 items]}},
"instagram": {{"caption": "2-3 short lines, a question to drive comments", "hashtags": ["#..", 10 items]}},
"youtube": {{"title": "max 70 chars, curiosity + keyword, ends with #shorts", "description": "2 sentences", "hashtags": ["#..", 3 items]}}}}""", temp=0.7)
        out = {}
        for k in ("tiktok", "instagram", "youtube"):
            v = d.get(k) or {}
            out[k] = {x: (clean_text(v.get(x), 600) if x != "hashtags" else [clean_text(h, 40) for h in (v.get(x) or [])][:12])
                      for x in ("caption", "title", "description", "hashtags") if v.get(x)}
        return out
    try:
        j["captions"] = await run_in_threadpool(work)
        save_history()
        return j["captions"]
    except Exception as e:
        return JSONResponse({"error": "Couldn't write captions right now. (" + redact(str(e))[:80] + ")"}, 503)


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
    if b.get("visuals") == "ai" and ai_gate(user):
        return JSONResponse({"error": ai_gate(user), "account": "/account"}, 403)
    script = clean_text(b.get("script"), 30000)
    words = len(script.split())
    if words < 150:
        return JSONResponse({"error": "Long Script Mode needs at least ~150 words. Use the normal mode for short videos."}, 400)
    parts = lf.split_parts(script)
    if b.get("dry"):
        out = {"parts": [{"words": len(p.split()), "start": p[:90]} for p in parts], "minutes": round(words / lf.WPM, 1)}
        if b.get("meta"):
            import stylepresets as sp
            pr = sp.get(preset_of({**b, "_long": True}))
            out["meta"] = await run_in_threadpool(lf.meta, clean_text(b.get("title"), 90) or script[:60], parts, sp.title_hint(pr) if pr else "")
        return out
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
    secs_in = b.get("sections") if isinstance(b.get("sections"), list) else []
    if len(secs_in) == len(parts) and b.get("yt_title"):
        st["meta"] = {"sections": [clean_text(x, 60) or f"Part {i + 1}" for i, x in enumerate(secs_in)],
                      "title": clean_text(b.get("yt_title"), 100), "description": clean_text(b.get("yt_desc"), 1500)}
    for k in ("sections", "yt_title", "yt_desc"):
        st.pop(k, None)
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
        mp = int(t.get("progress") or 0)
        dp = int(j.get("dprog") or 0)
        out["progress"] = min(99, (30 + mp * 70 // 100) if dp >= 30 else max(dp, mp if not dp else 0))
        out["stage"] = j.get("stage", "")
        out["notice"] = j.get("notice", "")
        out["flow"] = j.get("flow", "")
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
        d.update({x: y for x, y in status(k, req).items() if x in ("progress", "elapsed", "ahead", "stage", "notice")} if j["status"] in ("queued", "running") else {})
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
    acct = gd.account_by_key((j.get("drive") or {}).get("acct")) if j.get("drive") else None
    if acct is not None:  # stream from the owner's Drive (supports seeking via Range)
        from fastapi.responses import StreamingResponse
        try:
            r = acct.open_media(j["drive"]["id"], req.headers.get("range"))
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
    acct = gd.account_by_key((j.get("drive") or {}).get("acct")) if j and j.get("drive") else None
    if not j or current_user(req) != j.get("user") or not (j.get("drive") or {}).get("thumb") or acct is None:
        return JSONResponse({"error": "not found"}, 404)
    with acct.open_media(j["drive"]["thumb"]) as r:
        return HTMLResponse(r.read(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


ADMINS = {u.strip() for u in os.environ.get("ADMIN_USERS", "miracle").split(",") if u.strip()}


def _drive_state(u):
    if gd.user_connected(u):
        return {"mode": "own", "email": gd.user_email(u), "folder": gd.folder_name()}
    if gd.enabled():
        return {"mode": "admin", "folder": gd.folder_name() + "/users/" + u}
    return {"mode": "local"}


@app.get("/api/drive")
def api_drive(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    d = _drive_state(u)  # never includes tokens
    d["can_connect"] = gd.configured()
    return d


ACCOUNTP = ((HERE / "account.html").read_text().replace("{{SHELL}}", (HERE / "static/cc-shell.html").read_text())
            .replace("{{HEADER}}", (HERE / "static/cc-header.html").read_text()).replace("{{APP}}", "MoneyPrinter Studio"))


@app.get("/account", response_class=HTMLResponse)
def account_page(req: Request, msg: str = ""):
    u = current_user(req)
    if not u:
        return RedirectResponse("/login", 303)
    import html as _h
    r = HTMLResponse(ACCOUNTP.replace("{{USER}}", _h.escape(u)).replace("{{MSG}}", _h.escape(msg)).replace(
        "{{ADMIN}}", '<a class="btn ghost" href="/admin/drive">Admin Drive (fallback storage)</a>' if u in ADMINS else ""))
    r.headers["Cache-Control"] = "no-store"
    return r


@app.post("/api/drive/disconnect")
def drive_disconnect(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        ok = gd.disconnect_user(u)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "Could not disconnect: " + redact(e)[:80]}, 502)
    return {"ok": ok, **_drive_state(u)}


@app.get("/admin/drive", response_class=HTMLResponse)
def admin_drive(req: Request, saved: str = ""):
    if current_user(req) not in ADMINS:
        return HTMLResponse("Not allowed", 403)
    st = ("✅ Admin Drive connected — users who have not connected their own Drive are saved to “" + gd.folder_name() + "/users/&lt;name&gt;”, and the encrypted token vault lives here." if gd.enabled()
          else "⚪ Not connected." if gd.configured() else
          "⚪ Disabled: set GDRIVE_CLIENT_ID and GDRIVE_CLIENT_SECRET on Render first (see setup steps).")
    btn = '<p><a href="/oauth/google/start?mode=admin" style="background:#a855f7;color:#fff;padding:10px 16px;border-radius:10px;text-decoration:none">Connect admin Google Drive</a></p>' if gd.configured() else ""
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><body style="font-family:sans-serif;background:#0b0a1a;color:#e9e6ff;max-width:640px;margin:40px auto;padding:0 16px">
<h2>Admin Google Drive (fallback storage)</h2><p>{st}</p><p>Connected user Drives: {len(gd._users)}</p>{saved and '<p>'+saved+'</p>'}{btn}<p style="color:#9c97c0;font-size:13px">Scope: drive.file (the app can only see files it created). Redirect URI: {gd.redirect_uri()}</p><p><a href="/account" style="color:#a855f7">← Account</a></p>""")


@app.get("/oauth/google/start")
def oauth_start(req: Request, mode: str = "user"):
    u = current_user(req)
    if not u or not gd.configured() or (mode == "admin" and u not in ADMINS):
        return RedirectResponse("/account?msg=" + urllib.parse.quote("Google Drive is not set up on this server yet."), 303) if u else RedirectResponse("/login", 303)
    st = secrets.token_urlsafe(24)
    r = RedirectResponse(gd.auth_url(st), 303)
    r.set_cookie("gst", ("a" if mode == "admin" else "u") + "." + u + "." + st, max_age=600, httponly=True, samesite="lax",
                 secure=os.environ.get("RENDER") is not None)
    return r


@app.get("/oauth/google/callback", response_class=HTMLResponse)
def oauth_cb(req: Request, code: str = "", state: str = "", error: str = ""):
    u = current_user(req)
    ck = req.cookies.get("gst", "").split(".", 2)
    if not u or not gd.configured() or len(ck) != 3 or ck[1] != u:
        return HTMLResponse("Not allowed", 403)
    mode = "admin" if ck[0] == "a" and u in ADMINS else "user"

    def back(m):
        r = admin_drive(req, saved=m) if mode == "admin" else RedirectResponse("/account?msg=" + urllib.parse.quote(m), 303)
        r.delete_cookie("gst"); r.headers["Cache-Control"] = "no-store"
        return r
    if error or not code or not state or not hmac.compare_digest(state, ck[2]):
        return back("Google sign-in was cancelled or expired. Try again.")
    try:
        rt, at, _ = gd.exchange(code)
    except Exception as e:  # noqa: BLE001
        return back("Google did not accept the sign-in: " + redact(e)[:80])
    if mode == "user":
        if not rt:
            return back("Google did not return a long-lived token. Remove MoneyPrinter Studio at myaccount.google.com/permissions and connect again.")
        try:
            gd.connect_user(u, rt, gd.whoami(at))
        except Exception as e:  # noqa: BLE001
            return back("Could not save the connection: " + redact(e)[:80])
        for k, v in jobs.items():  # re-home this user's finished local videos into their own Drive
            if v.get("user") == u and v.get("status") == "done" and not v.get("drive") and Path(v.get("video") or "").exists():
                _up.push(k)
        _up.history_changed({u, gd.ADMIN})
        return back("Connected. New videos will be saved to your Google Drive.")
    if rt:
        gd.set_admin_refresh(rt)
    for k, v in jobs.items():
        if v.get("status") == "done" and not v.get("drive"):
            _up.push(k)
    _up.history_changed()
    note = ("✅ Connected for this server session. To keep it after restarts, copy this refresh token into Render → Environment as "
            "<b>GDRIVE_REFRESH_TOKEN</b> (shown once, keep it private):<br><textarea readonly style='width:100%;height:70px'>"
            + (rt or "(Google did not return a new refresh token — remove the app at myaccount.google.com/permissions and connect again)")
            + "</textarea>")
    return back(note)


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

# ---------------- Google Flow bridge ----------------
# Flow has no public API. We export Flow-ready prompts (VEO Automation extension imports .txt/.csv),
# and a small Mac helper (tools/flow-bridge) uploads the downloaded clips back with a per-job token.
import json as _json, csv as _csv, io as _io  # noqa: E402

FLOW_DIR = scn.UP_DIR.parent / "studio_flow"
FLOW_CAMS = ["slow push-in", "gentle pan left", "slow pull-back", "gentle pan right", "slow tilt up", "handheld tracking shot"]


def _fpath(fid):
    return FLOW_DIR / (re.sub(r"[^a-f0-9]", "", fid or "")[:32] + ".json")


def _fload(fid):
    p = _fpath(fid)
    try:
        return _json.loads(p.read_text()) if p.name != ".json" and p.exists() else None
    except Exception:
        return None


def _fsave(j):
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    _fpath(j["id"]).write_text(_json.dumps(j))


@app.post("/api/flow/job")
async def flow_job(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json(), 20000)
    texts = [clean_text((s or {}).get("text"), 400) for s in (b.get("scenes") or [])][:40]
    texts = [t for t in texts if t]
    if not texts:
        return JSONResponse({"error": "Plan the scenes first."}, 400)
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily AI limit reached ({CAPS['ai']} requests)."}, 429)
    style = aiv_norm(b.get("style"))
    aspect = "16:9" if b.get("aspect") == "16:9" else "9:16"
    import aivisuals as aiv, director

    def work():
        try:
            ps, meta = aiv.prompts(texts, clean_text(b.get("topic"), 300) or texts[0], style, director.gem, aspect=aspect, target="video")
        except Exception:
            ps, meta = [f"{t}. {aiv.STYLES[style]}" for t in texts], {}
        return ps, meta, 8

    prompts, meta, dur = await run_in_threadpool(work)
    kd = [str(k) for k in (b.get("kinds") or [])]
    kinds = [(kd[i] if i < len(kd) and kd[i] in ("image", "video") else ("video" if i == 0 else "image")) for i in range(len(texts))]
    token = secrets.token_urlsafe(24)
    j = {"id": uuid.uuid4().hex, "user": u, "th": hashlib.sha256(token.encode()).hexdigest(), "created": time.time(),
         "aspect": aspect, "duration": dur, "style": style, "texts": texts, "prompts": prompts, "meta": meta,
         "filled": [None] * len(texts), "kinds": kinds, "models": [_model(k, b.get("video_model") or _prefs(u).get("video_model")) for k in kinds]}
    if isinstance(b.get("adv"), dict):
        j["adv"] = flow_adv(b["adv"])
        j["models"] = [_model(k, adv=j["adv"]) for k in kinds]
    _fsave(j)
    return {"id": j["id"], "token": token, "kinds": kinds, "models": j["models"], "prompts": prompts, "scenes": len(texts), "aspect": aspect, "duration": dur}


def _fown(req, fid):
    u = current_user(req)
    j = _fload(fid)
    return j if (u and j and j["user"] == u) else None


@app.get("/api/flow/costs")
def flow_costs(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    m = _fmeta(u)
    return {**FLOW_COSTS, "balance": m.get("credits"), "balance_at": m.get("credits_at")}


@app.get("/api/flow/status")
def flow_status(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    m = _fmeta(u)
    seen = _FLOW_SEEN.get(u) or m.get("seen") or 0
    return {"connected": u in _keys(), "online": flow_online(u), "last_seen": seen or None,
            "label": m.get("label", ""), "device": m.get("device", ""), "paired": m.get("paired"),
            "stage": (_PSTAGE.get(u) or ("", 0))[0] if time.time() - (_PSTAGE.get(u) or ("", 0))[1] < 1800 else ""}


@app.get("/api/flow/engine")
def flow_engine_get(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    import aivisuals as aiv
    return {"engine": _prefs(u).get("engine", "flow") if is_admin(u) else "flow", "online": flow_online(u), "connected": u in _keys(),
            "free_models": is_admin(u), "ai_gate": ai_gate(u),
            "models": aiv.MODELS, "video_model": _prefs(u).get("video_model", "lite"), "image_model": _prefs(u).get("image_model", "pro"),
            "video_models": TIERS, "image_models": ITIERS}


# Google Flow credits per generation, from Google's help page "Manage your Google Flow credits"
# (support.google.com/flow/answer/16526234, read 2026-10-31). Images are not on Google's table; gflow-cli's docs
# (E2E_TESTING.md, confirmed with its maintainer 2026-09-02) say image generation spends 0 credits and has a daily cap.
FLOW_COSTS = {
    "video": {"lite": {"std": 10, "ultra": 5}, "fast": {"std": 20, "ultra": 10}, "quality": {"std": 100, "ultra": 100},
              "omni": {"by_sec": {"4": 7, "6": 10, "8": 12, "10": 15}}},
    "image": {"pro": 0, "nb2": 0, "lite": 0},
    "source": "https://support.google.com/flow/answer/16526234", "image_note": "0 credits (daily image quota, per gflow-cli docs; not on Google's table)",
}


@app.get("/api/flow/helper.zip")
def flow_helper_zip(req: Request):
    """'Download Flow Helper for Mac': a zip holding 'Connect Flow.command' with this server's URL and a
    single-use pairing token (30 min, bound to this user) baked in. Same claim route as the typed code."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    now = time.time()
    for h in [h for h, (_, e) in _PAIRS.items() if e < now]:
        _PAIRS.pop(h, None)
    tok = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(24))
    _PAIRS[hashlib.sha256(tok.encode()).hexdigest()] = (u, now + 1800)
    _PSTAGE[u] = ("downloaded", now)
    base = str(req.base_url).rstrip("/").replace("http://moneyprinter", "https://moneyprinter")
    tpl = (Path(__file__).resolve().parent.parent / "tools" / "flow-bridge" / "connect_template.command").read_text()
    body = tpl.replace("__STUDIO__", base).replace("__TOKEN__", tok)
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        zi = zipfile.ZipInfo("Connect Flow.command", time.localtime(now)[:6])
        zi.external_attr = (0o100755 << 16); zi.create_system = 3  # keeps the executable bit when Finder unzips it
        z.writestr(zi, body)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="Connect Flow.zip"', "Cache-Control": "no-store"})


@app.get("/api/flow/{fid}")
def flow_status(fid: str, req: Request):
    if fid == "engine":  # route order: /api/flow/{fid} is registered first
        return flow_engine_get(req)
    if fid == "status":
        return flow_status(req)
    j = _fown(req, fid)
    if not j:
        return JSONResponse({"error": "not found"}, 404)
    n = sum(1 for x in j["filled"] if x)
    return {"id": j["id"], "filled": j["filled"], "kinds": j.get("kinds"), "models": j.get("models"), "online": flow_online(j["user"]),
            "adv": flow_adv(j.get("adv") or {}), "sadv": [_sadv(j, i) for i in range(len(j["texts"]))],
            "prompts": [_fprompt(j, i) for i in range(len(j["prompts"]))], "done": n, "total": len(j["filled"]),
            "status": "ready" if n == len(j["filled"]) else f"Waiting for Flow clips {n}/{len(j['filled'])}"}


@app.get("/api/flow/{fid}/export.{fmt}")
def flow_export(fid: str, fmt: str, req: Request):
    j = _fown(req, fid)
    if not j:
        return JSONResponse({"error": "not found"}, 404)
    name = f"flow-{j['id'][:8]}"
    if fmt == "txt":  # VEO Automation: one prompt per paragraph (blank line between)
        body, ct = "\n\n".join(_fprompt(j, i).replace("\n", " ") for i in range(len(j["prompts"]))) + "\n", "text/plain"
    elif fmt == "lines":
        body, ct = "\n".join(p.replace("\n", " ") for p in j["prompts"]) + "\n", "text/plain"
        name += "-lines"; fmt = "txt"
    elif fmt == "csv":  # VEO Automation: prompt in the first column, header row skipped
        s = _io.StringIO(); w = _csv.writer(s)
        w.writerow(["prompt", "scene", "type", "model", "aspect_ratio", "duration_s", "outputs", "mode", "camera", "seed",
                    "audio", "upscale", "extend", "first_frame", "last_frame", "ingredients", "sentence"])
        for i in range(len(j["prompts"])):
            a = _sadv(j, i)
            w.writerow([_fprompt(j, i), i + 1, _kind(j, i), j.get("models", [""] * 99)[i], a["aspect"] or j["aspect"], a["duration"],
                        a["outputs"], a["mode"], a["camera"], a["seed"], "on" if a["audio"] else "off", a["upscale"],
                        "yes" if a["extend"] else "no", a["first_frame"], a["last_frame"], a["ingredients"], j["texts"][i]])
        body, ct = s.getvalue(), "text/csv"
    elif fmt == "json":  # job file for tools/flow-bridge (token is NOT stored server-side in clear; user pastes it once)
        base = str(req.base_url).rstrip("/").replace("http://", "https://") if "onrender.com" in str(req.base_url) else str(req.base_url).rstrip("/")
        body = _json.dumps({"job": j["id"], "upload_url": base + "/api/flow/upload", "aspect": j["aspect"], "duration": j["duration"],
                            "style": j["style"], "characters": j["meta"].get("cast", ""), "setting": j["meta"].get("world", ""),
                            "story_bible": j["meta"].get("bible", ""),
                            "scenes": [{"n": i + 1, "type": _kind(j, i), "model": j.get("models", [""] * 99)[i], "sentence": t,
                                        "prompt": _fprompt(j, i), "advanced": _sadv(j, i)} for i, t in enumerate(j["texts"])],
                            "advanced": flow_adv(j.get("adv") or {})}, indent=2)
        ct = "application/json"
    else:
        return JSONResponse({"error": "format"}, 400)
    return HTMLResponse(body, media_type=ct, headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})


CAMERA = ["", "static", "slow dolly in", "dolly out", "pan left", "pan right", "tilt up", "tilt down", "orbit", "crane up",
          "handheld", "tracking shot", "zoom in", "drone flyover", "POV"]
TIERS = {"lite": "Veo 3.1 - Lite", "fast": "Veo 3.1 - Fast", "quality": "Veo 3.1 - Quality", "omni": "Omni Flash"}
ITIERS = {"pro": "Nano Banana Pro", "nb2": "Nano Banana 2", "lite": "Nano Banana 2 Lite"}


def flow_adv(b, base=None):
    """Validated Google Flow 'Advanced' settings (job defaults, or a per-scene override on top of base)."""
    b = b if isinstance(b, dict) else {}
    a = dict(base or {"mode": "text", "camera": "", "seed": "", "negative": "", "outputs": 1, "tier": "lite", "itier": "pro",
                      "audio": False, "aspect": "", "duration": 8, "upscale": "none", "extend": False, "ingredients": "bible",
                      "first_frame": "", "last_frame": ""})
    if b.get("mode") in ("text", "frames", "ingredients"): a["mode"] = b["mode"]
    if b.get("camera") in CAMERA: a["camera"] = b["camera"]
    if "seed" in b: a["seed"] = re.sub(r"\D", "", str(b["seed"]))[:10]
    if "negative" in b: a["negative"] = clean_text(b["negative"], 300)
    if "outputs" in b: a["outputs"] = int(num(b["outputs"], 1, 1, 4, int))
    if b.get("tier") in TIERS: a["tier"] = b["tier"]
    if b.get("itier") in ITIERS: a["itier"] = b["itier"]
    if "audio" in b: a["audio"] = bool(b["audio"])
    if b.get("aspect") in ("", "9:16", "16:9", "1:1", "4:3", "3:4"): a["aspect"] = b.get("aspect", a["aspect"])
    if "duration" in b: a["duration"] = min((4, 6, 8, 10), key=lambda d: abs(d - num(b["duration"], 8, 4, 10, int)))
    if b.get("upscale") in ("none", "1080p", "4k"): a["upscale"] = b["upscale"]
    if "extend" in b: a["extend"] = bool(b["extend"])
    if b.get("ingredients") in ("none", "bible", "scene1"): a["ingredients"] = b["ingredients"]
    for k in ("first_frame", "last_frame"):
        if b.get(k) in ("", "prev", "next", "own"): a[k] = b.get(k, a[k])
    if a["tier"] == "omni" and a["duration"] not in (4, 6, 8, 10): a["duration"] = 8
    if a["tier"] != "omni" and a["duration"] == 10: a["duration"] = 8  # Veo 3.1: 4/6/8 s
    return a


def _sadv(j, i):
    return flow_adv((j.get("sadv") or {}).get(str(i)) or {}, flow_adv(j.get("adv") or {}))


def _kind(j, i):
    return (j.get("kinds") or ["image"] * 99)[i]


def _fprompt(j, i):
    """Image scenes drop the motion clause; video scenes keep motion, camera move and duration."""
    p, a = j["prompts"][i], _sadv(j, i)
    if _kind(j, i) == "image":
        p = re.sub(r"\. Motion: [^.]*", "", p)
    else:
        if a["camera"]:
            p = re.sub(r"; camera [^;.]*", "", p) + f" Camera move: {a['camera']}."
        p = re.sub(r"\b8 seconds\b", f"{a['duration']} seconds", p)
        if a["mode"] == "frames":
            p += " Animate from the start frame" + (" to the end frame" if a["last_frame"] else "") + "."
        if a["mode"] == "ingredients":
            p += " Use the reference images (ingredients) for the character and key props."
    if a["negative"]:
        p += f" Also avoid: {a['negative']}."
    return p


def _model(kind, vm=None, adv=None):
    import aivisuals as aiv
    if adv:
        return ITIERS.get(adv.get("itier"), "Nano Banana Pro") if kind == "image" else TIERS.get(adv.get("tier"), "Veo 3.1 - Lite")
    if kind == "image":
        return aiv.MODELS["image"]
    return TIERS.get(vm) or aiv.MODELS["video"]  # lite | fast | quality | omni (Omni Flash, up to 10 s, 7 refs)


def _keys():
    try:
        return _json.loads((FLOW_DIR / "keys.json").read_text())
    except Exception:
        k = {}
        try:  # free-plan disk is wiped on restart: hashed keys are mirrored to the studio Drive
            k = (gd.admin().get_json("flow_keys.json") or {}) if gd.enabled() else {}
            if k:
                FLOW_DIR.mkdir(parents=True, exist_ok=True); (FLOW_DIR / "keys.json").write_text(_json.dumps(k))
        except Exception:
            pass
        return k


def _keys_save(k):
    FLOW_DIR.mkdir(parents=True, exist_ok=True); (FLOW_DIR / "keys.json").write_text(_json.dumps(k))
    if gd.enabled():
        threading.Thread(target=lambda: _try(lambda: gd.admin().put_json("flow_keys.json", k)), daemon=True).start()


def _try(f):
    try:
        f()
    except Exception:
        pass


def _fmeta(u, **upd):
    """Per-user Flow link info: account label, paired time, last seen (no secrets)."""
    f = FLOW_DIR / "flowmeta.json"
    try:
        m = _json.loads(f.read_text())
    except Exception:
        m = {}
    if upd:
        m.setdefault(u, {}).update(upd)
        if upd.get("_drop"):
            m.pop(u, None)
        FLOW_DIR.mkdir(parents=True, exist_ok=True); f.write_text(_json.dumps(m))
    return m.get(u, {})


_PAIRS = {}  # sha256(code) -> (user, expiry); in memory only, 10 minutes


_FLOW_SEEN = {}


def _online(u):
    _FLOW_SEEN[u] = time.time()


def flow_online(u):
    return time.time() - _FLOW_SEEN.get(u, 0) < 120


def flow_engine(u, req):
    """Flow is the default visual engine when this user's bridge is online, unless they picked the free engine.
    Non-admins have no free engine: Flow is their only AI engine."""
    eng = (req.get("engine") or _prefs(u).get("engine", "flow")) if is_admin(u) else "flow"
    return eng == "flow" and req.get("visuals", "ai") == "ai" and flow_online(u)


def _prefs(u):
    try:
        return _json.loads((FLOW_DIR / f"prefs_{_ufx(u)}.json").read_text())
    except Exception:
        return {}


def _flow_score(fid, i, user, upid):
    try:
        import aivisuals as aiv, director
        p = scn.upload_path(user, upid)
        j = _fload(fid)
        sc, crit, _ = aiv.verify(aiv._jpeg(p), j["texts"][i], j["prompts"][i], director.gem)
        j = _fload(fid)
        if j["filled"][i] and j["filled"][i].get("id") == upid:
            j["filled"][i].update(score=sc, critique=crit); _fsave(j)
        print(f"flow: job {fid[:8]} scene {i + 1} score {sc}/10 ({j['models'][i]})", flush=True)
    except Exception as e:
        print(f"flow: score failed {type(e).__name__}", flush=True)


def flow_runner(user, jid, req):
    """Return a director callback that queues the scenes for the user's Mac bridge and waits for the clips."""
    def run(lines, prompts, meta):
        token = secrets.token_urlsafe(24)
        n = len(lines)
        scs = (meta or {}).get("scenes") or []
        want = [bool(i < len(scs) and isinstance(scs[i], dict) and scs[i].get("video")) for i in range(n)]
        if any(want):  # the director chose which shots need motion (capped: Veo spends Flow credits)
            cap_v, kinds = max(1, n // 5), []
            for i in range(n):
                kinds.append("video" if want[i] and kinds.count("video") < cap_v else "image")
        else:
            kinds = ["video" if (i == 0 and req.get("flow_video_hook", True)) else "image" for i in range(n)]
        wpm = 160
        try:
            import stylepresets as sp
            wpm = (sp.get(req.get("style_preset")) or {}).get("words_per_minute", 160)
        except Exception:
            pass
        sadv = {str(i): {"duration": max(4, round(len(lines[i].split()) / wpm * 60 + 0.5))} for i in range(n) if kinds[i] == "video"}
        prompts = list(prompts)
        for i in range(n):  # video shots: the director's motion + camera move go into the Veo prompt
            if kinds[i] == "video" and i < len(scs) and isinstance(scs[i], dict) and "Motion:" not in prompts[i]:
                prompts[i] = f"{prompts[i]}. Motion: {scs[i].get('motion') or 'subtle movement'}; camera {scs[i].get('camera_move') or 'slow push-in'}"[:1400]
        j = {"id": uuid.uuid4().hex, "user": user, "th": hashlib.sha256(token.encode()).hexdigest(), "created": time.time(),
             "aspect": req.get("aspect") or "9:16", "duration": 8, "style": req.get("ai_style") or "realistic", "texts": lines,
             "prompts": prompts, "meta": meta, "filled": [None] * n, "kinds": kinds, "auto": True, "render": jid,
             "models": [_model(k, req.get("video_model") or _prefs(user).get("video_model")) for k in kinds]}
        pf = _prefs(user)
        pick = {k: v for k, v in (("itier", req.get("flow_image_model") or pf.get("image_model")),
                                  ("tier", req.get("flow_video_model") or req.get("video_model") or pf.get("video_model"))) if v}
        if isinstance(req.get("flow_adv"), dict) or pick:  # model pickers (long-form / Shorts) or the scene editor panel
            j["adv"] = flow_adv(dict(req.get("flow_adv") if isinstance(req.get("flow_adv"), dict) else {}, **pick))
            j["models"] = [_model(k, adv=j["adv"]) for k in kinds]
        j["sadv"] = {i: flow_adv(v, flow_adv(j.get("adv") or {})) for i, v in sadv.items()}
        if req.get("flow_agent"):  # opt-in: pass the look + characters as a Flow Agent instruction card (free, no extra generations)
            try:
                import stylepresets as sp
                pr = sp.get(req.get("style_preset"))
            except Exception:
                pr = None
            j["agent_brief"] = clean_text(" ".join(x for x in ((pr or {}).get("visual_style", ""), (meta or {}).get("bible", "")) if x)
                                          + " Keep every recurring character's face, clothing and colours identical across scenes.", 600)
        _fsave(j)
        if jid in jobs:
            jobs[jid]["flow"] = j["id"]
        wait = int(os.environ.get("FLOW_WAIT", 900))
        t0 = time.time()
        while time.time() - t0 < wait:
            j = _fload(j["id"])
            got = sum(1 for x in j["filled"] if x)
            if jid in jobs:
                jobs[jid]["stage"] = f"Waiting for Google Flow clips {got}/{n}"
            if got == n or j.get("failed") or not flow_online(user):
                break
            time.sleep(5)
        out = {}
        for i, up in enumerate(j["filled"]):
            p = up and scn.upload_path(user, up["id"])
            if p:
                out[i] = {"path": str(p), "model": up.get("model") or "Google Flow", "video": up.get("video"), "score": up.get("score")}
        j["closed"] = True; _fsave(j)
        return out
    return run


def _bridge_user(req):
    tok = (req.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if not tok:
        return None
    th = hashlib.sha256(tok.encode()).hexdigest()
    return next((u for u, h in _keys().items() if hmac.compare_digest(h, th)), None)


@app.post("/api/flow/key")
async def flow_key(req: Request):
    """Create (or replace) this user's bridge key for the Mac helper. Shown once; only a hash is stored."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    key = "fb_" + secrets.token_urlsafe(24)
    k = _keys(); k[u] = hashlib.sha256(key.encode()).hexdigest(); _keys_save(k)
    _fmeta(u, paired=time.time(), label=_fmeta(u).get("label", ""), via="key")
    return {"key": key, "install": "curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh",
            "run": "flow-bridge --connect " + str(req.base_url).rstrip("/").replace("http://moneyprinter", "https://moneyprinter")}


@app.post("/api/flow/pair")
def flow_pair(req: Request):
    """'Connect my Google Flow': a one-time 8-character pairing code (10 min). Only its hash is kept."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    now = time.time()
    for h in [h for h, (_, e) in _PAIRS.items() if e < now or _PAIRS[h][0] == u]:
        _PAIRS.pop(h, None)
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    _PAIRS[hashlib.sha256(code.encode()).hexdigest()] = (u, now + 600)
    base = str(req.base_url).rstrip("/").replace("http://moneyprinter", "https://moneyprinter")
    return {"code": code[:4] + "-" + code[4:], "expires_in": 600,
            "install": "curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh",
            "run": f"flow-bridge --connect {base} --pair"}


_PSTAGE = {}  # user -> (stage, time) reported by the one-click file before it pairs


@app.post("/api/flow/progress")
async def flow_progress(req: Request):
    """The one-click file reports how far it got (installing / signin / pairing). Token is checked, not used up."""
    b = clean_body(await req.json(), 40)
    tok = re.sub(r"[^A-Z0-9]", "", str(b.get("token", "")).upper())
    hit = _PAIRS.get(hashlib.sha256(tok.encode()).hexdigest())
    if not hit or hit[1] < time.time():
        return JSONResponse({"error": "expired"}, 403)
    st = str(b.get("stage", ""))
    if st in ("started", "installing", "signin", "pairing"):
        _PSTAGE[hit[0]] = (st, time.time())
    return {"ok": True}


@app.post("/api/flow/claim")
async def flow_claim(req: Request):
    """Called by the helper on the user's own computer: swaps a pairing code for that user's bridge key."""
    ip = (req.headers.get("x-forwarded-for") or (req.client.host if req.client else "")).split(",")[0].strip()
    now = time.time()
    f = [t for t in _fails.get("pair:" + ip, []) if now - t < 900]
    if len(f) >= 10:
        return JSONResponse({"error": "Too many wrong codes. Wait 15 minutes."}, 429)
    b = clean_body(await req.json(), 80)
    code = re.sub(r"[^A-Z0-9]", "", str(b.get("code", "")).upper())
    hit = _PAIRS.pop(hashlib.sha256(code.encode()).hexdigest(), None)
    if not hit or hit[1] < now:
        _fails["pair:" + ip] = f + [now]
        return JSONResponse({"error": "That code is wrong or expired. Make a new one on the Account page."}, 403)
    u = hit[0]
    key = "fb_" + secrets.token_urlsafe(24)
    k = _keys(); k[u] = hashlib.sha256(key.encode()).hexdigest(); _keys_save(k)  # replaces any older helper for this user
    _fmeta(u, paired=now, label=clean_text(b.get("label"), 80), device=clean_text(b.get("device"), 60), via="code")
    return {"key": key, "user": u}


@app.delete("/api/flow/link")
def flow_unlink(req: Request):
    """Unlink: the helper's key stops working at once; the user falls back to the free models."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    k = _keys(); k.pop(u, None); _keys_save(k)
    _FLOW_SEEN.pop(u, None); _fmeta(u, _drop=True)
    return {"ok": True}


@app.post("/api/flow/engine")
async def flow_engine_set(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json(), 20)
    pr = _prefs(u)
    if b.get("engine") in ("flow", "free"):
        pr["engine"] = b["engine"]
    if b.get("video_model") in TIERS:
        pr["video_model"] = b["video_model"]
    if b.get("image_model") in ITIERS:
        pr["image_model"] = b["image_model"]
    FLOW_DIR.mkdir(parents=True, exist_ok=True); (FLOW_DIR / f"prefs_{_ufx(u)}.json").write_text(_json.dumps(pr))
    return pr


@app.post("/api/flow/ping")
async def flow_ping(req: Request):
    """Mac helper heartbeat + work queue: returns open jobs (auto renders and Send-to-Flow jobs) that still need clips."""
    u = _bridge_user(req)
    if not u:
        return JSONResponse({"error": "bad bridge key"}, 403)
    _online(u)
    try:
        cr = (await req.json()).get("credits")
        if isinstance(cr, int) and 0 <= cr < 10 ** 7 and (cr != _fmeta(u).get("credits") or time.time() - _fmeta(u).get("credits_at", 0) > 1800):
            _fmeta(u, credits=cr, credits_at=time.time())
    except Exception:
        pass
    lab = clean_text(req.headers.get("x-flow-account"), 80)
    if lab or time.time() - _fmeta(u).get("seen", 0) > 600:
        _fmeta(u, seen=time.time(), **({"label": lab} if lab else {}))
    out = []
    if FLOW_DIR.exists():
        for f in sorted(FLOW_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)[-50:]:
            if f.name.startswith(("keys", "prefs_", "chars_", "music_", "flowmeta")):
                continue
            j = _fload(f.stem)
            if j and j["user"] == u and not j.get("closed") and time.time() - j["created"] < 7 * 86400 and not all(j["filled"]):
                out.append({"job": j["id"], "upload_url": str(req.base_url).rstrip("/") + "/api/flow/upload", "aspect": j["aspect"],
                            "duration": j["duration"], "auto": bool(j.get("auto")), "story_bible": j["meta"].get("bible", ""), "agent_brief": j.get("agent_brief", ""),
                            "references": [{"name": r, "url": "/char/" + r} for r in sorted({r for rs in (j["meta"].get("char_refs") or []) for r in rs})],
                            "scenes": [{"n": i + 1, "type": _kind(j, i), "model": j.get("models", [""] * 99)[i], "sentence": t,
                                        "prompt": _fprompt(j, i), "advanced": _sadv(j, i), "filled": bool(j["filled"][i]),
                                        "characters": (j["meta"].get("chars") or [[]] * 99)[i] if i < len(j["meta"].get("chars") or []) else []} for i, t in enumerate(j["texts"])]})
    return {"user": u, "jobs": out}


@app.post("/api/flow/{fid}/adv")
async def flow_adv_set(fid: str, req: Request):
    """Job-wide Advanced defaults; per-scene overrides stay on top."""
    j = _fown(req, fid)
    if not j:
        return JSONResponse({"error": "not found"}, 404)
    j["adv"] = flow_adv(clean_body(await req.json(), 400))
    j["models"] = [_model(_kind(j, i), adv=_sadv(j, i)) for i in range(len(j["texts"]))]
    _fsave(j)
    return {"ok": True, "adv": j["adv"], "models": j["models"]}


@app.post("/api/flow/{fid}/scene")
async def flow_scene_set(fid: str, req: Request):
    """Per-scene override from the Send to Flow panel: type (image/video), model, edited prompt."""
    j = _fown(req, fid)
    if not j:
        return JSONResponse({"error": "not found"}, 404)
    b = clean_body(await req.json(), 1400)
    i = int(num(b.get("i"), 0, 0, len(j["texts"]) - 1, int))
    if b.get("type") in ("image", "video"):
        j.setdefault("kinds", ["image"] * len(j["texts"]))[i] = b["type"]
        j.setdefault("models", [""] * len(j["texts"]))[i] = _model(b["type"], b.get("video_model"))
    if b.get("model"):
        j["models"][i] = clean_text(b["model"], 60)
    if b.get("prompt"):
        j["prompts"][i] = clean_text(b["prompt"], 1400)
    if isinstance(b.get("adv"), dict):
        j.setdefault("sadv", {})[str(i)] = {k: v for k, v in flow_adv(b["adv"], _sadv(j, i)).items()}
        if not b.get("model"):
            j["models"][i] = _model(_kind(j, i), adv=_sadv(j, i))
    _fsave(j)
    return {"ok": True, "type": _kind(j, i), "model": j["models"][i], "prompt": _fprompt(j, i), "adv": _sadv(j, i)}


@app.post("/api/flow/upload")
async def flow_upload(req: Request, file: UploadFile = File(...)):
    """Token-auth upload from the flow-bridge helper. Fields: job, optional scene (1-based); header Authorization: Bearer <token>."""
    form = await req.form()
    j = _fload(str(form.get("job") or ""))
    tok = (req.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    th = hashlib.sha256(tok.encode()).hexdigest()
    if not j or not tok or not (hmac.compare_digest(j["th"], th) or hmac.compare_digest(_keys().get(j["user"], "x"), th)):
        return JSONResponse({"error": "bad job or token"}, 403)
    if time.time() - j["created"] > 7 * 86400:
        return JSONResponse({"error": "token expired (7 days)"}, 403)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"):
        return JSONResponse({"error": "Only MP4/MOV/WEBM or JPG/PNG/WEBP."}, 415)
    try:
        sc = int(form.get("scene") or 0)
    except ValueError:
        sc = 0
    if sc < 1:
        sc = next((i + 1 for i, x in enumerate(j["filled"]) if not x), 0)
    if not 1 <= sc <= len(j["filled"]):
        return JSONResponse({"error": "all scenes already filled", "done": len(j["filled"])}, 409)
    try:
        if not use(j["user"], "flow"):
            return JSONResponse({"error": f"Daily Flow limit reached ({CAPS['flow']} files). Try again tomorrow."}, 429)
        up = await run_in_threadpool(scn.save_upload, j["user"], file.filename, file.file)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    j = _fload(j["id"]); up["model"] = (j.get("models") or [""] * len(j["filled"]))[sc - 1]
    up["video"] = ext in (".mp4", ".mov", ".webm")
    threading.Thread(target=_flow_score, args=(j["id"], sc - 1, j["user"], up["id"]), daemon=True).start()
    j["filled"][sc - 1] = up; _fsave(j); _online(j["user"])
    n = sum(1 for x in j["filled"] if x)
    return {"ok": True, "scene": sc, "done": n, "total": len(j["filled"])}


import charlib  # noqa: E402
CHL = charlib.Lib(FLOW_DIR, _ufx, gd)


def chars_for(req):
    """Saved characters for this render: the ids the user ticked, or all of them when 'characters' is not sent."""
    try:
        cs = CHL.load(req.get("_user", ""))
    except Exception:
        return []
    ids = req.get("characters")
    return [c for c in cs if c["id"] in ids] if isinstance(ids, list) else cs


@app.get("/api/characters")
def chars_list(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    return {"characters": CHL.load(u), "drive": bool(gd.target_for(u)[1])}


@app.post("/api/characters")
async def chars_save(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    try:
        return CHL.upsert(u, clean_body(await req.json(), 600), clean_text)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)


@app.delete("/api/characters/{cid}")
def chars_delete(cid: str, req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    CHL.delete(u, re.sub(r"[^a-f0-9]", "", cid)[:10])
    return {"ok": True}


@app.post("/api/characters/{cid}/ref")
async def chars_ref(cid: str, req: Request, file: UploadFile = File(...)):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    data = await file.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        return JSONResponse({"error": "Reference images must be under 8 MB."}, 400)
    try:
        return await run_in_threadpool(CHL.add_ref, u, cid, data)
    except KeyError:
        return JSONResponse({"error": "not found"}, 404)
    except Exception:
        return JSONResponse({"error": "Upload a JPG, PNG or WEBP image."}, 400)


@app.post("/api/characters/{cid}/generate")
async def chars_generate(cid: str, req: Request):
    """'Generate reference': one portrait from the character's locked description (free image model)."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    c = next((x for x in CHL.load(u) if x["id"] == cid), None)
    if not c:
        return JSONResponse({"error": "not found"}, 404)
    if not is_admin(u):
        return JSONResponse({"error": "“Generate reference” uses the free image models, which are for the studio owner only. Upload 1–3 reference photos instead, or make one in your Google Flow.", "account": "/account"}, 403)
    if not use(u, "ai"):
        return JSONResponse({"error": f"Daily AI limit reached ({CAPS['ai']} requests)."}, 429)
    import aivisuals as aiv

    def work():
        path, prov = aiv.generate(CHL.portrait_prompt(c), 768, 1024, aiv.seed_for(c["name"]))
        return CHL.add_ref(u, cid, Path(path).read_bytes()), prov
    try:
        c2, prov = await run_in_threadpool(work)
    except Exception as e:
        return JSONResponse({"error": "The free image models are busy. Try again in a minute. (" + type(e).__name__ + ")"}, 503)
    return dict(c2, provider=prov)


@app.get("/char/{name}")
def chars_ref_get(name: str, req: Request):
    u = current_user(req) or _bridge_user(req)  # the Mac bridge fetches refs as Flow 'Ingredients'
    p = CHL.ref_path(u, name) if u else None
    return FileResponse(p, media_type="image/jpeg") if p else JSONResponse({"error": "not found"}, 404)


MOODS = ["calm", "uplifting", "epic", "dark", "suspense", "happy", "sad", "corporate", "lofi", "afrobeats", "cinematic", "ambient"]


@app.get("/api/music/tags")
def music_tags_get(req: Request):
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    t = CHL.load(u, "music")
    return {"tags": t[0] if t and isinstance(t[0], dict) else {}, "moods": MOODS}


@app.post("/api/music/tags")
async def music_tags_set(req: Request):
    """Mood / genre tag for one of the user's tracks (built-in tracks are untagged)."""
    u = current_user(req)
    if not u:
        return JSONResponse({"error": "not logged in"}, 401)
    b = clean_body(await req.json(), 200)
    t = (CHL.load(u, "music") or [{}])[0]
    mid = str(b.get("id") or "")[:120]
    if not mid.startswith("up:"):
        return JSONResponse({"error": "Only your own tracks can be tagged."}, 400)
    t[mid] = {"mood": b.get("mood") if b.get("mood") in MOODS else "", "genre": clean_text(b.get("genre"), 30)}
    CHL.save(u, [t], "music")
    return {"ok": True, "tags": t}


def _music_to_drive(u, path):
    def push():
        try:
            _, acct = gd.target_for(u)
            if acct:
                acct.upload(str(path), "studio_music_" + Path(path).name, "audio/mpeg", acct.find("studio_music_" + Path(path).name))
        except Exception:
            pass
    threading.Thread(target=push, daemon=True).start()


def _music_from_drive(u, name):
    try:
        _, acct = gd.target_for(u)
        fid = acct.find("studio_music_" + name) if acct else None
        if fid:
            dst = Path(bgm_service.uploaded_bgm_dir()) / name
            acct.download(fid, str(dst))
            return str(dst)
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
