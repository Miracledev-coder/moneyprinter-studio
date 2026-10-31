"""Flow scene scheduling: per-scene attempts, stall detection, failure reports and safe scoring.

Pure functions over a Flow job dict (no I/O) so they can be unit-tested without the web server.
Job keys used: filled (list), models, kinds, created, att {"i": n}, att_t {"i": t}, gave {"i": reason},
prog_t (last progress on this job: an upload or the helper reporting it is working on it), why {"i": last error}.
"""
import os
import subprocess
import time
import traceback

MAX_ATT = int(os.environ.get("FLOW_MAX_ATTEMPTS", 3))
STALL = int(os.environ.get("FLOW_STALL", 180))              # no progress on a missing scene for this long -> re-queue it
SCENE_MAX = {"image": int(os.environ.get("FLOW_SCENE_MAX_IMAGE", 360)),  # helper 'working' on one scene longer than this
             "video": int(os.environ.get("FLOW_SCENE_MAX_VIDEO", 600))}  # counts as stuck even while it reports progress


def att(j, i):
    return int((j.get("att") or {}).get(str(i), 1))


def init(j, now=None):
    now = now or time.time()
    n = len(j["filled"])
    j.setdefault("att", {str(i): 1 for i in range(n)})
    j.setdefault("att_t", {str(i): now for i in range(n)})
    j.setdefault("gave", {}); j.setdefault("why", {})
    j.setdefault("prog_t", now)
    return j


def pending(j):
    """Scenes still wanted: not filled and not given up."""
    return [i for i, x in enumerate(j["filled"]) if not x and str(i) not in (j.get("gave") or {})]


def record_fail(j, i, reason, now=None, attempt=None, max_att=None):
    """A scene failed (helper report or stall). Returns ('retry', new_attempt) / ('giveup', n) / ('stale', cur) / ('done', 0)."""
    init(j, now); now = now or time.time(); max_att = max_att or MAX_ATT
    if j["filled"][i]:
        return "done", 0
    cur = att(j, i)
    if str(i) in j["gave"]:
        return "giveup", cur
    if attempt is not None and int(attempt) < cur:  # an older attempt's failure arriving late: already re-queued
        return "stale", cur
    j["why"][str(i)] = str(reason or "unknown")[:300]
    if cur >= max_att:
        j["gave"][str(i)] = f"Flow failed {cur} attempt(s): {str(reason or 'unknown')[:200]}"
        return "giveup", cur
    j["att"][str(i)] = cur + 1; j["att_t"][str(i)] = now; j["prog_t"] = now
    return "retry", cur + 1


def note_progress(j, work=None, now=None):
    """work = (job_id, scene_n(1-based), since) the helper says it is generating right now (or None)."""
    now = now or time.time()
    if work and work[0] == j["id"]:
        j["prog_t"] = max(j.get("prog_t") or 0, now)


def stalled(j, now=None, work=None, stall=None):
    """Scenes to re-queue: missing and no progress on this job for `stall` s since the scene was (re)dispatched,
    or the helper has been stuck on that one scene longer than its kind's SCENE_MAX."""
    init(j, now); now = now or time.time(); stall = stall or STALL
    out = []
    for i in pending(j):
        since = max(float(j["att_t"].get(str(i), j.get("created", now))), float(j.get("prog_t") or 0))
        if work and work[0] == j["id"] and int(work[1]) == i + 1:
            kind = (j.get("kinds") or ["image"] * 99)[i]
            if now - max(float(work[2]), float(j["att_t"].get(str(i), 0))) > SCENE_MAX.get(kind, 600):
                out.append((i, f"helper stuck on it for over {SCENE_MAX.get(kind, 600)} s"))
            continue
        if now - since > stall:
            out.append((i, f"no result {int(now - since)} s after dispatch/last progress"))
    return out


def frame_jpeg(path, timeout=60):
    """One scaled JPEG frame of an image or video (videos: ~1 s in, falling back to the first frame)."""
    path = str(path)
    for pre in (["-ss", "1"], []):
        r = subprocess.run(["ffmpeg", "-loglevel", "error", *pre, "-i", path, "-vf", "scale=384:-2", "-frames:v", "1",
                            "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True, timeout=timeout)
        if r.stdout:
            return r.stdout
    return b""


def score(j, i, path, verify, gem, log=print, is_video=None):
    """Gemini check of one Flow upload. Never raises; logs a traceback on failure. Returns (score, critique) or None."""
    try:
        if not j or not path:
            log(f"flow: score skipped scene {i + 1}: {'job' if not j else 'file'} missing"); return None
        texts, prompts = j.get("texts") or [], j.get("prompts") or []
        line = texts[i] if i < len(texts) and texts[i] else ""
        prompt = prompts[i] if i < len(prompts) and prompts[i] else ""
        img = frame_jpeg(path)
        if not img:
            log(f"flow: score skipped scene {i + 1}: no frame could be read from {os.path.basename(str(path))}"); return None
        res = verify(img, line, prompt, gem) or ()
        sc = res[0] if len(res) > 0 else None
        crit = res[1] if len(res) > 1 else ""
        return (float(sc) if sc is not None else -1.0), str(crit or "")
    except Exception:
        log("flow: score failed\n" + traceback.format_exc())
        return None
