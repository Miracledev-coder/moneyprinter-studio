"""Mac-side rendering: hand the final compose of a Short to the user's paired Flow helper, with a server fallback.

Flow of one render:
  server (prepare)  -> spec + inputs (voice mp3, subtitles, fonts, music, watermark, end card, scene files)
  helper long-polls /api/render/next and claims it instantly, reuses Flow files it already has (sha256 match),
  downloads the rest, runs fastrender (VideoToolbox), posts progress (the reply carries Stop), uploads the MP4.
  If the helper is not polling, does not claim within CLAIM_S, goes quiet for BEAT_S or reports a failure,
  the server renders the same spec itself with fastrender (libx264).
"""
import hashlib, json, os, shutil, threading, time, uuid
from pathlib import Path

import fastrender

CLAIM_S = float(os.environ.get("MAC_RENDER_CLAIM_S", 12))   # helper is long-polling: a claim normally takes < 1 s
BEAT_S = float(os.environ.get("MAC_RENDER_BEAT_S", 90))     # no progress for this long = helper died
POLL_S = float(os.environ.get("MAC_RENDER_POLL_S", 25))     # long-poll hold time
SEEN_S = 60                                                  # helper counts as ready if it polled this recently
MAX_UPLOAD = 1 << 30

_LOCK = threading.Condition()
_JOBS = {}       # rid -> RJob
_SEEN = {}       # user -> last /api/render/next time
_POLLING = {}    # user -> number of open long-polls


class Fallback(Exception):
    pass


class RJob:
    def __init__(self, user, jid, spec, files, workdir):
        self.rid = uuid.uuid4().hex
        self.user, self.jid, self.spec, self.files, self.workdir = user, jid, spec, files, workdir
        self.state, self.claimed, self.beat, self.error = "queued", 0.0, time.time(), ""
        self.pct, self.msg, self.timings, self.result, self.cancel = 0, "", {}, None, False
        self.inputs = []
        for name, p in files.items():
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            self.inputs.append({"name": name, "size": os.path.getsize(p), "sha256": h.hexdigest()})


def helper_ready(user):
    return bool(_POLLING.get(user)) or time.time() - _SEEN.get(user, 0) < SEEN_S


def enabled():
    return os.environ.get("MAC_RENDER", "1") != "0"


def dispatch(user, jid, spec, files, workdir, dest, cancel=lambda: False, progress=lambda pct, msg: None,
             hint="", claim_s=None, beat_s=None):
    """Try the Mac. Returns the helper's timings dict once `dest` holds the MP4; raises Fallback otherwise."""
    if not enabled() or not helper_ready(user):
        raise Fallback("helper not online")
    j = RJob(user, jid, spec, files, workdir)
    j.hint = hint
    with _LOCK:
        _JOBS[j.rid] = j
        _LOCK.notify_all()
    claim_s, beat_s = claim_s or CLAIM_S, beat_s or BEAT_S
    t0 = time.time()
    try:
        while True:
            with _LOCK:
                _LOCK.wait(0.5)
            if cancel():
                j.cancel = True
                time.sleep(1.2)  # let the helper see Stop on its next progress post
                raise fastrender.Cancelled()
            if j.state == "queued" and time.time() - t0 > claim_s:
                raise Fallback("helper did not claim the render")
            if j.state == "claimed" and time.time() - j.beat > beat_s:
                raise Fallback("helper went quiet")
            if j.state == "failed":
                raise Fallback("helper failed: " + j.error[:200])
            if j.state == "claimed":
                progress(j.pct, j.msg)
            if j.state == "done":
                shutil.move(j.result, dest)
                return j.timings
    finally:
        with _LOCK:
            if j.state in ("queued", "claimed"):
                j.state, j.cancel = "abandoned", True
            # keep the record a little so a late helper call gets a clean "cancel" answer
            tmr = threading.Timer(300, lambda r=j.rid: _JOBS.pop(r, None)); tmr.daemon = True; tmr.start()


def _own(user, rid):
    j = _JOBS.get(rid)
    return j if j and j.user == user else None


def install(app, bridge_user, bad_key):
    from fastapi import Request
    from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
    import asyncio

    @app.get("/api/render/engine.py")
    async def render_engine():
        return PlainTextResponse(Path(fastrender.__file__).read_text(), headers={"X-Engine-Version": fastrender.ENGINE_VERSION})

    @app.post("/api/render/next")
    async def render_next(req: Request):
        u = bridge_user(req)
        if not u:
            return bad_key(req)
        _SEEN[u] = time.time()
        _POLLING[u] = _POLLING.get(u, 0) + 1
        try:
            end = time.time() + min(POLL_S, float(req.query_params.get("wait", POLL_S)))
            while True:
                with _LOCK:
                    j = next((x for x in _JOBS.values() if x.user == u and x.state == "queued"), None)
                    if j:
                        j.state, j.claimed, j.beat = "claimed", time.time(), time.time()
                        _LOCK.notify_all()
                if j:
                    return {"rid": j.rid, "job": j.jid, "spec": j.spec, "inputs": j.inputs, "hint": getattr(j, "hint", ""),
                            "engine": fastrender.ENGINE_VERSION}
                if time.time() >= end or await req.is_disconnected():
                    return {"rid": None}
                await asyncio.sleep(0.25)
        finally:
            _POLLING[u] -= 1
            _SEEN[u] = time.time()

    @app.get("/api/render/{rid}/file/{name:path}")
    async def render_file(rid: str, name: str, req: Request):
        u = bridge_user(req)
        j = _own(u, rid) if u else None
        if not j or name not in j.files:
            return JSONResponse({"error": "not found"}, 404)
        j.beat = time.time()
        return FileResponse(j.files[name])

    @app.post("/api/render/{rid}/progress")
    async def render_progress(rid: str, req: Request):
        u = bridge_user(req)
        j = _own(u, rid) if u else None
        if not j:
            return {"cancel": True}
        try:
            b = await req.json()
        except Exception:
            b = {}
        j.beat, j.pct, j.msg = time.time(), int(b.get("pct") or j.pct), str(b.get("msg") or j.msg)[:120]
        return {"cancel": j.cancel or j.state != "claimed"}

    @app.post("/api/render/{rid}/fail")
    async def render_fail(rid: str, req: Request):
        u = bridge_user(req)
        j = _own(u, rid) if u else None
        if not j:
            return {"ok": True}
        try:
            j.error = str((await req.json()).get("error") or "failed")[:400]
        except Exception:
            j.error = "failed"
        with _LOCK:
            if j.state == "claimed":
                j.state = "failed"
            _LOCK.notify_all()
        return {"ok": True}

    @app.post("/api/render/{rid}/result")
    async def render_result(rid: str, req: Request):
        """Raw MP4 body (streamed to disk, never held in RAM); X-Render-Timings header = JSON timings."""
        u = bridge_user(req)
        j = _own(u, rid) if u else None
        if not j or j.state != "claimed":
            return JSONResponse({"error": "render is no longer waiting", "cancel": True}, 409)
        out = os.path.join(j.workdir, "mac_result.mp4")
        n = 0
        with open(out, "wb") as f:
            async for chunk in req.stream():
                n += len(chunk)
                if n > MAX_UPLOAD:
                    return JSONResponse({"error": "too large"}, 413)
                f.write(chunk)
                j.beat = time.time()
        with open(out, "rb") as f:
            head = f.read(12)
        if n < 1000 or head[4:8] != b"ftyp":
            return JSONResponse({"error": "not an mp4"}, 400)
        try:
            j.timings = json.loads(req.headers.get("x-render-timings") or "{}")
        except Exception:
            j.timings = {}
        with _LOCK:
            j.result, j.state = out, "done"
            _LOCK.notify_all()
        return {"ok": True}


# ---------- server side: build the spec from the director's deferred plan ----------

def prepare(plan, params, workdir, audio, sub_maker, script, log=print):
    """Collect every input in `workdir` and return (spec, files{name: path}). Uses MoneyPrinter's own subtitle code."""
    from app.services import video as vid, voice as vs
    from app.utils import utils
    import re
    wd = Path(workdir); wd.mkdir(parents=True, exist_ok=True)
    W, H = plan["W"], plan["H"]
    files = {}
    clips = []
    for k, c in enumerate(plan["clips"]):
        src = c.get("path")
        if not src and c.get("url"):  # stock footage: fetched once, here, for both renderers
            src = str(wd / f"stock{k:02d}.mp4")
            import urllib.request
            with urllib.request.urlopen(urllib.request.Request(c["url"], headers={"User-Agent": "Mozilla/5.0"}), timeout=90) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
        if not src or not os.path.exists(src):
            continue
        name = f"scene{k:02d}{Path(src).suffix.lower() or '.bin'}"
        files[name] = src
        clips.append({k2: c[k2] for k2 in ("dur", "still", "kb", "zoom", "anim", "fade") if k2 in c} | {"name": name})
    files["voice.mp3"] = audio
    spec = {"W": W, "H": H, "look": plan.get("look") or "neutral", "xf": plan.get("xf", 0.4), "clips": clips,
            "audio": "voice.mp3", "voice_volume": params.voice_volume or 1.0, "end": 2.5,
            "hd": bool(getattr(params, "output_hd", False))}
    if params.subtitle_enabled and sub_maker is not None:
        srt = str(wd / "subtitle.srt")
        vs.create_subtitle(text=script, sub_maker=sub_maker, subtitle_file=srt,
                           word_level=getattr(params, "subtitle_display_mode", "sentence") == "word_by_word")
        if os.path.exists(srt):
            f = vid.subtitle_filter(params, srt, W, H)
            m = re.search(r"force_style='([^']*)'", f)
            ass = os.path.splitext(srt)[0] + ".preset.ass"
            if not m and os.path.exists(ass):
                files["subtitle.ass"] = ass; spec["sub"] = {"file": "subtitle.ass"}
            else:
                files["subtitle.srt"] = srt; spec["sub"] = {"file": "subtitle.srt", "force_style": m.group(1) if m else ""}
            font = os.path.join(utils.font_dir(), params.font_name or "")
            if os.path.isfile(font):
                files["fonts/" + os.path.basename(font)] = font
    if params.bgm_type:
        try:
            bgm = vid.get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file) or ""
        except Exception:
            bgm = ""
        if bgm and os.path.exists(bgm):
            files["music" + Path(bgm).suffix] = bgm
            spec.update(bgm="music" + Path(bgm).suffix, bgm_volume=params.bgm_volume or (0.3 if params.duck_music else 0.2),
                        duck=bool(getattr(params, "duck_music", False)),
                        fade_in=float(getattr(params, "music_fade_in", 1.5) or 0), fade_out=float(getattr(params, "music_fade_out", 2.5) or 0))
    wm = getattr(params, "watermark_file", "") or ""
    if wm and os.path.exists(wm):
        files["wm.png"] = wm; spec["wm"] = "wm.png"
    end_txt = (getattr(params, "end_card_text", "") or "").strip()[:40]
    if end_txt:
        card = str(wd / "endcard.png")
        vid.make_end_card(card, W, H, end_txt, wm if wm and os.path.exists(wm) else "")
        files["endcard.png"] = card; spec["endcard"] = "endcard.png"
    return spec, files


def stage_local(files, workdir):
    """Server fallback: make every input reachable by its spec name inside workdir (symlinks, no copies)."""
    for name, p in files.items():
        dst = Path(workdir) / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.resolve() != Path(p).resolve():
            dst.unlink(missing_ok=True)
            try:
                os.symlink(os.path.abspath(p), dst)
            except OSError:
                shutil.copy(p, dst)
