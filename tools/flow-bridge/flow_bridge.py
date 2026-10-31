#!/usr/bin/env python3
"""flow-bridge: send Google Flow clips from your Mac straight into MoneyPrinter Studio scenes.

Default (safe) route: you run the prompts in Flow with the free VEO Automation extension, it downloads
the clips, and this helper uploads each new file to the next empty scene of your Studio job.

  python3 flow_bridge.py --job ~/Downloads/flow-xxxx.json          # asks for the upload token once
  python3 flow_bridge.py --job flow-xxxx.json --folder ~/Downloads/my-project

Experimental: --drive uses gflow-cli (https://github.com/ffroliva/gflow-cli, unofficial) to drive Flow
with your own signed-in Chrome, at a human pace (about 15 s between prompts, configurable) with a daily cap of 50. Use at your own risk.
No password is ever asked for or stored. Python standard library only.
"""
import argparse, getpass, json, mimetypes, os, random, re, shutil, subprocess, sys, time, urllib.parse, urllib.request, uuid
from pathlib import Path

MEDIA = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
STATE = Path.home() / ".flow-bridge"
# Flow model-picker names -> gflow-cli --model aliases, checked against `gflow models --json` (gflow-cli 0.92.1, Oct 2026).
# Override with GFLOW_MODEL_MAP='{"Nano Banana Pro":"nano-pro"}' if a later gflow renames them.
GFLOW_MODELS = {"Nano Banana Pro": "nano-pro", "Nano Banana 2": "nano2", "Nano Banana 2 Lite": "nano2-lite", "Imagen 4": "image4",
                "Veo 3.1 - Lite": "veo-lite", "Veo 3.1 - Fast": "veo-fast", "Veo 3.1 - Quality": "veo-quality", "Omni Flash": "omni-flash"}
BATCH_MODELS = {"nano2", "nano2.1", "nano-2.1", "nano-pro", "nano2-lite", "image4", "imagen4"}
GFLOW_MODELS.update(json.loads(os.environ.get("GFLOW_MODEL_MAP", "{}") or "{}"))


def upload(url, job, token, path, scene=0):
    b = uuid.uuid4().hex
    ct = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="job"\r\n\r\n{job}\r\n'.encode()]
    if scene:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="scene"\r\n\r\n{scene}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\nContent-Type: {ct}\r\n\r\n'.encode()
                 + path.read_bytes() + f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}", "Authorization": f"Bearer {token}",
                                          "User-Agent": "flow-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read()).get("error", str(e)), "code": e.code}
        except Exception:
            return {"error": str(e), "code": e.code}


def stable(p):
    """Wait until a download has finished (size stops changing, no .crdownload)."""
    last = -1
    for _ in range(120):
        if not p.exists():
            return False
        s = p.stat().st_size
        if s == last and s > 0:
            return True
        last = s
        time.sleep(2)
    return False


def scene_from_name(name, total):
    """VEO Automation's auto-rename starts files with the prompt index (e.g. 03_lagos-traffic.mp4)."""
    m = re.match(r"^\D{0,12}?(\d{1,3})[\s_\-.]", name)
    n = int(m.group(1)) if m else 0
    return n if 1 <= n <= total else 0


def describe(a, kind):
    """Human-readable Flow settings to pick by hand in Flow / VEO Automation (they have no import field)."""
    out = [f"{a.get('outputs', 1)} output(s)"]
    if a.get("aspect"):
        out.append("aspect " + a["aspect"])
    if kind == "video":
        out += [f"{a.get('duration', 8)}s", {"text": "Text to Video", "frames": "Frames to Video (start/end frame)",
                                            "ingredients": "Ingredients to Video"}.get(a.get("mode"), "Text to Video")]
        if a.get("camera"):
            out.append("camera: " + a["camera"])
        out.append("audio " + ("on" if a.get("audio") else "off (Studio mutes clip audio)"))
        if a.get("extend"):
            out.append("then Extend in Scene builder")
    if a.get("seed"):
        out.append("seed " + a["seed"] + " (not settable in Flow; kept for your records)")
    if a.get("upscale") not in (None, "none"):
        out.append("download as " + a["upscale"].upper() + " upscale")
    return ", ".join(out)


for _s in (sys.stdout, sys.stderr):  # Windows consoles (cp1252) must not crash on ✓ · … in messages
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


def patch_gflow():
    """gflow-cli 0.92.1 fetches Flow's signed media URL with max_redirects=0, but Flow's migrated host now answers
    with a 302 to its Google CDN, so every finished clip 'fails' to download. Allow a few redirects (the signed URL is
    already checked to be a Google host). Idempotent; re-applied after every gflow upgrade."""
    py = Path.home() / ".local/share/uv/tools/gflow-cli" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        d = subprocess.run([str(py) if py.exists() else sys.executable, "-c", "import gflow_cli,os;print(os.path.dirname(gflow_cli.__file__))"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
        n = 0
        for rel in ("api/transports/migrated_composer.py", "api/transports/migrated_recover.py"):
            f = Path(d) / rel
            if f.exists():
                t = f.read_text()
                u = re.sub(r'(route="flow-content\.google",\s*)max_redirects=0,', r"\1max_redirects=3,", t)
                if u != t:
                    f.write_text(u); n += 1
                    for c in (f.parent / "__pycache__").glob(f.stem + ".*.pyc"):
                        c.unlink(missing_ok=True)
        if n:
            print(f"[auto] patched gflow-cli download redirects ({n} file(s))", flush=True)
    except Exception as e:
        print(f"[auto] could not patch gflow-cli downloads ({type(e).__name__})", flush=True)


def gflow_bin():
    for c in (shutil.which("gflow"), str(Path.home() / ".local/bin/gflow"), str(Path.home() / ".local/bin/gflow.exe")):
        if c and Path(c).exists():
            return c
    return None


DRIFT = 23  # gflow-cli exit code: Flow's page changed (selector drift) -> stop driving, fall back to manual


CTX = {}  # base + key while connected, so the driver can ask whether a job was stopped


def job_open(jid):
    """False once Studio stopped/closed this job (Stop generation): no more Flow credits are spent on it."""
    if not CTX:
        return True
    try:
        return any(j["job"] == jid for j in ping(CTX["base"], CTX["key"]).get("jobs", []))
    except Exception:
        return True


STATUS = {"state": "idle", "msg": ""}
TASK_TIMEOUT = int(os.environ.get("FLOW_TASK_TIMEOUT", 600))  # hard limit per Flow generation


def hstat(state, msg):
    """What the helper is doing right now; sent with every heartbeat so Studio's progress panel can show it."""
    STATUS.update(state=state, msg=msg[:200], at=int(time.time()))


def _run(cmd, folder):
    """Run gflow, echo its output live, keep the tail in r.out so failures can be classified."""
    tail = []
    try:
        pr = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        t0 = time.time()
        for line in pr.stdout:
            print(line, end="", flush=True); tail.append(line); del tail[:-300]
            if time.time() - t0 > TASK_TIMEOUT:
                raise subprocess.TimeoutExpired(cmd, TASK_TIMEOUT)
        pr.wait(timeout=max(5, TASK_TIMEOUT - (time.time() - t0)))
        r = subprocess.CompletedProcess(cmd, pr.returncode)
    except subprocess.TimeoutExpired:
        try:
            pr.kill()
        except Exception:
            pass
        print(f"[auto] Flow did not finish this generation within {TASK_TIMEOUT // 60} min; giving up on it (it stays open for a manual upload).", flush=True)
        hstat("error", f"A Flow generation timed out after {TASK_TIMEOUT // 60} min")
        r = subprocess.CompletedProcess(cmd, 124)
    r.out = "".join(tail)
    return r


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def download_failed(out):
    """True when the clip/image WAS generated (and billed) but only the download failed."""
    return bool(re.search(r"MediaDownloadError|signed media URL|data download|no signed media URL|download returned HTTP", out or ""))


def model_rejected(out):
    """Only a real 'model not available/accepted' error may fall back to another model."""
    return not download_failed(out) and bool(re.search(r"(unknown|unsupported|invalid|not available|not accepted|no such|unrecognized)[^\n]{0,40}model|model[^\n]{0,60}(unknown|unsupported|not available|not accepted|invalid|unavailable)", out or "", re.I))


def media_ids(out):
    """Media ids gflow names in a download error (dedup, in order)."""
    ids = []
    for line in (out or "").splitlines():
        if re.search(r"media|download|data download", line, re.I):
            for m in UUID.findall(line):
                if m.lower() not in ids:
                    ids.append(m.lower())
    return ids


def recover(gflow, mid, out, tries=4):
    """`gflow data download <id>`: Flow issues a fresh signed link for an already-billed clip. Spends no credits."""
    tmp = STATE / "rec" / mid; shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
    for i in range(tries):
        print(f"[auto] recovering media {mid[:8]} with `gflow data download` (no credits) … try {i + 1}/{tries}", flush=True)
        r = _run([gflow, "data", "download", mid, "--out", str(tmp)], str(tmp))
        got = sorted((p for p in tmp.glob("*.*") if p.stat().st_size > 0), key=lambda p: p.stat().st_mtime)
        if r.returncode == 0 and got:
            dst = out.with_suffix(got[-1].suffix); shutil.move(str(got[-1]), str(dst)); return dst
        if "exit 11" in r.out or "Video only" in r.out:
            break  # gflow can only re-fetch videos by id
        time.sleep((20, 60, 120, 180)[min(i, 3)])
    return None


def _pending(update=None):
    f = STATE / "pending-downloads.json"
    cur = json.loads(f.read_text()) if f.exists() else []
    if update is not None:
        STATE.mkdir(exist_ok=True); f.write_text(json.dumps(update, indent=1)); return update
    return cur


def recover_pending(gflow):
    """Retry downloads of clips that were generated (billed) but never landed. Run on start and via --recover."""
    left = []
    for it in _pending():
        out = Path(it["out"])
        if out.exists() or list(out.parent.glob(out.stem + ".*")):
            continue
        if not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        got = recover(gflow, it["media_id"], out, tries=2)
        print(f"[auto] {'recovered ' + got.name if got else 'still could not download ' + it['media_id'][:8]}", flush=True)
        if not got:
            left.append(it)
    _pending(left)
    return left


PROJECT_TITLE = "MoneyPrinter Studio"


def _cfg(update=None):
    f = STATE / "config"
    c = json.loads(f.read_text()) if f.exists() else {}
    if update:
        c.update(update); STATE.mkdir(exist_ok=True); f.write_text(json.dumps(c, indent=1))
    return c


def flow_project(gflow, fresh=False):
    """One Flow project for everything: find or create 'MoneyPrinter Studio', remember its id in ~/.flow-bridge/config."""
    pid = None if fresh else _cfg().get("project_id")
    if pid:
        return pid
    try:
        if not fresh:
            r = subprocess.run([gflow, "project", "list", "--json", "--limit", "200"], capture_output=True, text=True, timeout=60)
            for p in (json.loads(r.stdout or "{}").get("projects") or []):
                if p.get("title") == PROJECT_TITLE:
                    pid = p["project_id"]; break
        if not pid:
            r = subprocess.run([gflow, "project", "create", "--name", PROJECT_TITLE, "--json"], capture_output=True, text=True, timeout=300)
            pid = json.loads(r.stdout or "{}").get("project_id")
    except Exception as e:
        print(f"[auto] could not set up the '{PROJECT_TITLE}' Flow project ({type(e).__name__}); gflow will use its own", flush=True)
    if pid:
        _cfg({"project_id": pid}); print(f"[auto] using Flow project '{PROJECT_TITLE}' ({pid})", flush=True)
    return pid


def project_gone(out):
    return bool(re.search(r"project[^\n]{0,80}(not found|does not exist|deleted|404|no longer)", out or "", re.I))


def drive(job, folder, cap, pace=None):
    """Route A/B: gflow-cli drives labs.google/flow in a real, headed Chrome with your own signed-in profile.
    One scene at a time, ~15 s apart (Studio Account > Flow, --pace or FLOW_PACE), daily cap. Each file is saved as NN_scene.ext so it lands in the right scene."""
    env = os.environ.get("FLOW_PACE")
    if not pace and env:
        pace = tuple(max(10, int(x)) for x in (env.split(",") * 2)[:2])
    if not pace and job.get("pace"):
        b = max(10, int(job["pace"])); pace = (max(10, b - 2), b + 3)
    pace = pace or (13, 18)
    gflow = gflow_bin()
    if not gflow:
        print("[auto] gflow-cli is not installed; using the manual route (VEO Automation / drag-and-drop)."); return False
    told = False
    while subprocess.run([gflow, "auth", "status"], capture_output=True).returncode:  # wait (no restart needed) until Flow is signed in
        hstat("signin", "Flow isn't signed in on your Mac. Run: flow-bridge --signin")
        if not told:
            print("[auto] Flow is not signed in on this computer. Run:  flow-bridge --signin   in another Terminal window."
                  " This helper keeps the job and continues by itself once you are signed in.", flush=True); told = True
        if not job_open(job["job"]):
            print(f"[auto] Job {job['job'][:8]} was stopped or timed out in Studio."); job["stopped"] = True; hstat("idle", ""); return True
        time.sleep(20)
    if told:
        print("[auto] Flow is signed in now; continuing the job.", flush=True)
    STATE.mkdir(exist_ok=True)
    cf = STATE / f"count-{time.strftime('%Y-%m-%d')}.txt"
    used = int(cf.read_text() or 0) if cf.exists() else 0
    refs = sorted(str(p) for p in Path(folder).glob("ref-*"))
    t0 = time.time()
    batch = [sc for sc in job["scenes"] if not sc.get("filled") and sc.get("type") == "image" and not sc.get("characters")
             and GFLOW_MODELS.get(sc.get("model", ""), "nano2") in BATCH_MODELS][:max(0, cap - used)]
    if len(batch) > 1 and os.environ.get("FLOW_BATCH", "1") != "0":
        # One `gflow run` = ONE Chrome session for all plain image scenes (gflow-cli locks a profile to one process, so
        # parallel gflow processes would fail with "Profile locked"). Saves the ~30-60 s browser start per scene.
        brief = (job.get("agent_brief") or "")[:400]
        rows = [{"text": ((brief + "\n\n") if brief else "") + sc["prompt"][:1500], "aspect_ratio": job["aspect"],
                 "model": GFLOW_MODELS.get(sc.get("model", ""), "nano2"), "count": 1,
                 "output_filename": f"{sc['n']:02d}_scene.png"} for sc in batch]
        cfgf = STATE / "batch.json"; cfgf.write_text(json.dumps({"prompts": rows}))
        cmd = [gflow, "run", "--config", str(cfgf), "--output-dir", str(folder), "--continue-on-error", "--jitter", f"{pace[0]}-{pace[1]}"]
        proj = flow_project(gflow)
        if proj:
            cmd += ["--project", proj]
        hstat("working", f"Generating {len(batch)} image scenes in one Flow session ({', '.join(str(s['n']) for s in batch)})")
        print(f"[auto] batch: {len(batch)} image scenes in one Flow session, {pace[0]}-{pace[1]} s apart …", flush=True)
        r = _run(cmd, folder)
        if r.returncode and proj and project_gone(r.out):
            np = flow_project(gflow, fresh=True)
            if np:
                cmd[cmd.index("--project") + 1] = np; r = _run(cmd, folder)
        for sc in batch:
            if (Path(folder) / f"{sc['n']:02d}_scene.png").exists():
                sc["filled"] = True; used += 1
        cf.write_text(str(used))
        print(f"[auto] batch done in {int(time.time() - t0)} s: {sum(1 for s in batch if s.get('filled'))}/{len(batch)} images"
              + ("" if all(s.get("filled") for s in batch) else "; the rest are retried one by one"), flush=True)
    for sc in job["scenes"]:
        if not job_open(job["job"]):
            print(f"[auto] Job {job['job'][:8]} was stopped in Studio; skipping its remaining scenes."); job["stopped"] = True; return True
        if used >= cap:
            print(f"[auto] Daily cap of {cap} Flow generations reached. The rest can be done by hand or tomorrow."); return True
        if sc.get("filled"):
            continue
        kind, model = sc.get("type", "video"), sc.get("model", "")
        a = sc.get("advanced") or {}
        alias = GFLOW_MODELS.get(model, "")
        my = [r for r in refs if any(c.lower() in r.lower() for c in sc.get("characters") or [])] or (refs if sc.get("characters") else [])
        ext = ".png" if kind == "image" else ".mp4"
        out = Path(folder) / f"{sc['n']:02d}_scene{ext}"
        if kind == "image":
            cmd = [gflow, "image", "i2i" if my else "t2i", sc["prompt"], "--aspect", job["aspect"], "-n", "1"]
            for r in my[:3]:
                cmd += ["--ref", r]
            tmp = STATE / "tmp"; shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
            cmd += ["-o", str(out)] if not my else ["--out", str(tmp)]
        else:
            dur = int(a.get("duration") or job.get("duration") or 8)
            dur = str(min(dur, 10 if "omni" in str(model).lower() else 8))  # Veo 3.1 tops out at 8 s, Omni Flash at 10 s
            dur = dur if dur in ("4", "6", "8", "10") else "8"
            cmd = [gflow, "video", "r2v" if my else "t2v", sc["prompt"], "--aspect", job["aspect"], "--duration", dur, "-o", str(out)]
            for r in my[:3]:
                cmd += ["--ref", r]
        agent = kind == "image" and job.get("agent_brief")
        if agent:  # optional: Flow Agent instruction card for this generation (free; keeps style + characters consistent)
            cmd += ["-i", job["agent_brief"][:600]]
        proj = flow_project(gflow)
        if proj:
            cmd += ["--project", proj]
        if alias:
            cmd += ["--model", alias]
        hstat("working", f"Generating scene {sc['n']}/{len(job['scenes'])}: {kind} · {model or 'Flow default'} · {int(time.time() - t0)} s so far")
        print(f"[auto] scene {sc['n']}: {kind} · {model or 'Flow default'}{' · ' + str(len(my)) + ' character ref(s)' if my else ''} …", flush=True)
        r = _run(cmd, folder)
        if r.returncode == DRIFT:
            print("[auto] Flow's page changed and gflow-cli can't find its buttons (exit 23). Stopping automation;"
                  " finish the remaining scenes by hand (prompts.txt) or update:  uv tool upgrade gflow-cli"); return False
        if r.returncode and proj and project_gone(r.out) and not download_failed(r.out):
            print(f"[auto] the '{PROJECT_TITLE}' Flow project is gone; creating it again")
            np = flow_project(gflow, fresh=True)
            if np:
                cmd[cmd.index("--project") + 1] = np; r = _run(cmd, folder)
        if r.returncode and download_failed(r.out):  # generated + billed; only the download failed: NEVER regenerate
            ids, got = media_ids(r.out), None
            print(f"[auto] scene {sc['n']} was generated but its download failed; fetching it again by id (no credits)", flush=True)
            for mid in ids:
                got = recover(gflow, mid, out)
                if got:
                    break
            tries = 0
            while not got and tries < 2 and used + 1 < cap and job_open(job["job"]):  # re-download failed: regenerate, SAME model
                tries += 1; used += 1; cf.write_text(str(used))
                print(f"[auto] scene {sc['n']}: re-download failed; regenerating with the same model ({tries}/2)", flush=True)
                hstat("working", f"Scene {sc['n']}: regenerating ({tries}/2) with {model or 'Flow default'}")
                time.sleep(random.randint(*pace))
                r = _run(cmd, folder)
                if out.exists() and not r.returncode:
                    got = out
                elif download_failed(r.out):
                    for mid in media_ids(r.out):
                        got = recover(gflow, mid, out) if kind != "image" else None
                        if got:
                            break
                    ids = media_ids(r.out) or ids
            if got:
                r = subprocess.CompletedProcess(cmd, 0); r.out = ""
            else:
                hstat("error", f"Scene {sc['n']} failed after re-download + 2 regenerations. Use 'Regenerate scene' in Studio.")
                _pending(_pending() + [{"job": job["job"], "scene": sc["n"], "media_id": m, "out": str(out)} for m in ids[:1]])
                print(f"[auto] saved media id(s) {', '.join(m[:8] for m in ids) or '?'}; run  flow-bridge --recover  later (no credits)", flush=True)
        elif r.returncode and r.returncode not in (DRIFT, 124) and agent and not out.exists() and not model_rejected(r.out):
            print("[auto] Flow Agent instruction was not accepted on this account; retrying without it")
            i = cmd.index("-i"); cmd = cmd[:i] + cmd[i + 2:]
            r = _run(cmd, folder)
        if r.returncode and alias and model_rejected(r.out):
            print(f"[auto] '{alias}' was not accepted; retrying with Flow's default model")
            i = cmd.index("--model"); r = _run(cmd[:i] + cmd[i + 2:], folder)
        if not out.exists() and kind == "image" and my:  # i2i wrote into tmp: move it in with the scene number
            new = sorted((STATE / "tmp").glob("*.*"), key=lambda p: p.stat().st_mtime)
            if new:
                shutil.move(str(new[-1]), str(out.with_suffix(new[-1].suffix)))
        k = 0
        while r.returncode and r.returncode not in (DRIFT,) and not out.exists() and not download_failed(r.out) \
                and not model_rejected(r.out) and k < 2 and used + 1 < cap and job_open(job["job"]):
            k += 1; used += 1
            print(f"[auto] scene {sc['n']} failed (exit {r.returncode}); regenerating with the same model ({k}/2)", flush=True)
            hstat("working", f"Scene {sc['n']}: retry {k}/2 with {model or 'Flow default'}")
            time.sleep(random.randint(*pace)); r = _run(cmd, folder)
        used += 1; cf.write_text(str(used))
        if r.returncode and not out.exists():
            hstat("error", f"Scene {sc['n']} failed after retries. Use 'Regenerate scene' in Studio.")
            print(f"[auto] scene {sc['n']} failed (exit {r.returncode}); it stays open for a manual upload.")
        if not any(not x.get("filled") and x["n"] > sc["n"] for x in job["scenes"]):
            continue
        wait = random.randint(*pace)
        hstat("pacing", f"Scene {sc['n']} done · next in {wait}s (human pace)")
        print(f"[auto] pausing {wait}s so Flow sees a human pace"); time.sleep(wait)
    hstat("idle", "")
    return True


KEYF = Path.home() / ".flow-bridge" / "key"
BASEF = Path.home() / ".flow-bridge" / "base"
LOGF = Path.home() / ".flow-bridge" / "helper.log"
LABEL = os.environ.get("FLOW_ACCOUNT", "")
VERSION = "2026.10.31.7"
DEFAULT_BASE = "https://moneyprinter-studio.onrender.com"
RAW = "https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/flow_bridge.py"
AGENT = "com.calmcraze.flowbridge"
PLIST = Path.home() / "Library" / "LaunchAgents" / (AGENT + ".plist")
WIN_STARTUP = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Flow Helper.vbs"


def _wrapper():
    w = Path.home() / ".local" / "bin" / ("flow-bridge.cmd" if os.name == "nt" else "flow-bridge")
    return w


def autostart(on, base=None):
    """Run the helper in the background at login and restart it if it stops (macOS LaunchAgent / Windows Startup)."""
    import platform
    base = base or (BASEF.read_text().strip() if BASEF.exists() else DEFAULT_BASE)
    STATE.mkdir(mode=0o700, exist_ok=True)
    if platform.system() == "Darwin":
        uid = str(os.getuid())
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{AGENT}"], capture_output=True)
        if not on:
            PLIST.unlink(missing_ok=True); print("Auto-start is OFF (background helper removed)."); return True
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        h = str(Path.home())
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{AGENT}</string>
<key>ProgramArguments</key><array><string>{_wrapper()}</string><string>--connect</string><string>{base}</string><string>--background</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{h}/.local/bin:{h}/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string><key>HOME</key><string>{h}</string></dict>
<key>WorkingDirectory</key><string>{h}</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>30</integer>
<key>ProcessType</key><string>Interactive</string>
<key>StandardOutPath</key><string>{LOGF}</string>
<key>StandardErrorPath</key><string>{LOGF}</string>
</dict></plist>
""")
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["launchctl", "load", "-w", str(PLIST)], capture_output=True)
        print(f"Auto-start is ON: the helper now runs in the background at login and restarts itself. Log: {LOGF}")
        return True
    if os.name == "nt":
        cmd = STATE / "autostart.cmd"
        if not on:
            WIN_STARTUP.unlink(missing_ok=True); cmd.unlink(missing_ok=True)
            subprocess.run(["taskkill", "/F", "/FI", "WINDOWTITLE eq FlowHelperBG*"], capture_output=True)
            print("Auto-start is OFF (background helper removed)."); return True
        cmd.write_text(f'@echo off\r\ntitle FlowHelperBG\r\n:loop\r\ncall "{_wrapper()}" --connect {base} --background >> "{LOGF}" 2>&1\r\nif exist "{cmd}" (timeout /t 30 /nobreak >nul & goto loop)\r\n')
        WIN_STARTUP.parent.mkdir(parents=True, exist_ok=True)
        WIN_STARTUP.write_text(f'CreateObject("WScript.Shell").Run """{cmd}""", 0, False\r\n')
        subprocess.Popen(["wscript.exe", str(WIN_STARTUP)])
        print(f"Auto-start is ON: the helper now runs hidden in the background at sign-in and restarts itself. Log: {LOGF}")
        return True
    print("Auto-start is only set up on macOS and Windows."); return False


def autostart_state():
    if PLIST.exists():
        r = subprocess.run(["launchctl", "list", AGENT], capture_output=True)
        return "on (running)" if r.returncode == 0 else "on (installed, not running)"
    if WIN_STARTUP.exists():
        return "on"
    return "off"


def unpair(why=""):
    """Forget this computer's key and remove auto-start."""
    if PLIST.exists() or WIN_STARTUP.exists():
        if why:  # we may be running inside the LaunchAgent: remove the file first so launchd stops restarting us
            PLIST.unlink(missing_ok=True); WIN_STARTUP.unlink(missing_ok=True); (STATE / "autostart.cmd").unlink(missing_ok=True)
        autostart(False)
    KEYF.unlink(missing_ok=True)
    print(why or "This computer is unpaired. Pair again any time from Studio → Account → Connect my Google Flow.")


_LOCK = []


def _busy_exit(bg):
    if bg:
        return False
    print("The helper is already running in the background on this computer, so Studio is connected. You can close this window.", flush=True)
    sys.exit(0)


def single_instance(bg=False):
    """Only one connected helper per computer; a second one (e.g. the background one) waits until the first stops."""
    STATE.mkdir(mode=0o700, exist_ok=True)
    f = open(STATE / "helper.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1); break
                except OSError:
                    _busy_exit(bg); time.sleep(15)
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _busy_exit(bg)
                print("Another helper window is open; the background helper takes over when it closes.", flush=True)
                fcntl.flock(f, fcntl.LOCK_EX)
    except SystemExit:
        raise
    except Exception:
        pass
    _LOCK.append(f)


def self_update():
    """Fetch the latest helper from GitHub; if newer, replace this file and restart (keeps the key and pairing)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(RAW + f"?v={int(time.time())}", headers={"User-Agent": "flow-bridge/" + VERSION}), timeout=30) as r:
            src = r.read().decode()
        m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
        me = Path(__file__).resolve()
        v = lambda x: tuple(int(p) for p in x.split(".") if p.isdigit())
        if not m or v(m.group(1)) <= v(VERSION) or me.parent != STATE.resolve():
            return
        compile(src, "flow_bridge.py", "exec")
        tmp = me.with_suffix(".new"); tmp.write_text(src, encoding="utf-8"); os.replace(tmp, me)
        print(f"Updated the helper to {m.group(1)}; restarting…", flush=True)
        for f in _LOCK:
            f.close()
        os.execv(sys.executable, [sys.executable, str(me)] + sys.argv[1:])
    except Exception as e:
        print("(update check skipped:", type(e).__name__, ")", flush=True)


def pair(base, code, label):
    """Swap the one-time code from Studio → Account → Connect my Google Flow for this account's bridge key."""
    import platform
    body = json.dumps({"code": code, "label": label, "device": platform.node()[:60]}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/flow/claim", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "flow-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(json.loads(e.read() or b"{}").get("error") or f"Pairing failed ({e.code}).")
    KEYF.parent.mkdir(mode=0o700, exist_ok=True)
    BASEF.write_text(base.rstrip("/"))
    KEYF.write_text(d["key"])
    try:
        KEYF.chmod(0o600)
    except OSError:
        pass  # Windows: the file sits in the user's own profile folder  # stays on this computer only; never printed
    print(f"Paired with Studio account '{d['user']}'. Keep Google Flow signed in to YOUR account in Chrome.")
    return d["key"]


CREDITS = {"n": None, "at": 0}


def read_credits():
    """Flow credit balance via `gflow credits user --json` (reads the number in Flow's account menu; spends nothing)."""
    g = gflow_bin()
    if not g:
        return
    try:
        d, err = None, ""
        for extra in ([], ["--refresh"]):  # --refresh renews a signed-out session headlessly, then reads again
            r = subprocess.run([g, "credits", "user", "--json", *extra], capture_output=True, text=True, timeout=240)
            out, err = r.stdout or "", (r.stderr or "").strip()
            i = out.find("{")
            if i >= 0:
                try:
                    d = json.loads(out[i:out.rfind("}") + 1])
                except ValueError:
                    d = None
            if d is None:  # plain-text fallback: "Credits: 1234"
                m = re.search(r"credits?\D{0,20}?(\d[\d,]*)", out + "\n" + err, re.I)
                if m:
                    d = {"credits": int(m.group(1).replace(",", ""))}
            if isinstance(d, dict) and (d.get("error") or d.get("ok") is False or d.get("authenticated") is False):
                err = str(d.get("error") or d.get("message") or d)[:300]; d = None
            if d is not None:
                break
        if d is None:
            tail = (err or out).strip().splitlines()[-1:] or ["no output"]
            hint = (" -> Flow looks signed out in the helper's Chrome. Run: flow-bridge --signin"
                    if re.search(r"auth|sign|login|session|expired|401|403", err + out, re.I) else "")
            print(f"(could not read Flow credits: {tail[0][:200]}){hint}")
            return
        def find(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and any(w in k.lower() for w in ("credit", "balance", "remaining")):
                        return int(v)
                    r = find(v)
                    if r is not None:
                        return r
            if isinstance(x, list):
                for v in x:
                    r = find(v)
                    if r is not None:
                        return r
        n = find(d)
        if n is not None:
            CREDITS["n"] = n
            print(f"Flow credits left: {n}")
    except Exception as e:
        print(f"(could not read Flow credits: {type(e).__name__})")


def credits_loop():
    while True:
        read_credits(); CREDITS["at"] = time.time(); time.sleep(1800)


def ping(base, key):
    body = json.dumps(dict({"credits": CREDITS["n"]} if CREDITS["n"] is not None else {}, status=STATUS)).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/flow/ping", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "flow-bridge/" + VERSION, "X-Flow-Version": VERSION,
                                          **({"X-Flow-Account": LABEL} if LABEL else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# ---------------- Mac rendering: Studio hands the final compose of a Short to this computer ----------------
RENDER_DIR = STATE / "render"
FFMPEG_LOCAL = STATE / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
RENDER_DISK_CAP = 1 << 30  # keep render temp files under ~1 GB


def _fetch_ffmpeg():
    """Self-updated helpers that never re-ran the installer: fetch the same static evermeet.cx ffmpeg (no compiling)."""
    if sys.platform != "darwin":
        return
    import io, zipfile
    try:
        with urllib.request.urlopen(urllib.request.Request("https://evermeet.cx/ffmpeg/getrelease/zip", headers={"User-Agent": "flow-bridge/" + VERSION}), timeout=300) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        FFMPEG_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        tmp = FFMPEG_LOCAL.with_suffix(".tmp")
        tmp.write_bytes(z.read("ffmpeg")); tmp.chmod(0o755); os.replace(tmp, FFMPEG_LOCAL)
        print("Installed ffmpeg for fast rendering on this Mac.", flush=True)
    except Exception as e:
        print("(could not install ffmpeg:", type(e).__name__, ")", flush=True)


def _ffmpeg():
    if FFMPEG_LOCAL.exists() and os.access(FFMPEG_LOCAL, os.X_OK):
        return str(FFMPEG_LOCAL)
    return shutil.which("ffmpeg")


def _api(base, key, path, body=None, method=None, timeout=60, headers=None, raw=False):
    data = json.dumps(body).encode() if body is not None and not raw else body
    h = {"Authorization": f"Bearer {key}", "User-Agent": "flow-bridge/" + VERSION, "X-Flow-Version": VERSION, **(headers or {})}
    if body is not None and not raw:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method or ("POST" if body is not None else "GET"), headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _engine(base, key, want):
    """fastrender.py comes from Studio itself, so the Mac always renders with the server's exact version."""
    import importlib.util
    f = STATE / "fastrender.py"
    have = re.search(r'^ENGINE_VERSION = "([^"]+)"', f.read_text(), re.M) if f.exists() else None
    if not have or have.group(1) != want:
        src = _api(base, key, "/api/render/engine.py").decode()
        compile(src, "fastrender.py", "exec")
        f.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("fastrender", f)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _sha(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _local_copies(folder, hint, inputs):
    """Flow files the helper already downloaded (same size + sha256): no need to fetch them back from Studio."""
    want = {i["size"]: [] for i in inputs if i["name"].startswith("scene")}
    for i in inputs:
        if i["name"].startswith("scene"):
            want[i["size"]].append(i)
    found, dirs = {}, []
    if hint and (Path(folder) / f"flow-{hint[:8]}").is_dir():
        dirs.append(Path(folder) / f"flow-{hint[:8]}")
    try:  # then the most recent Flow job folders
        dirs += sorted((d for d in Path(folder).glob("flow-*") if d.is_dir() and d not in dirs), key=lambda d: -d.stat().st_mtime)[:6]
    except OSError:
        pass
    for d in dirs:
        for f in d.iterdir():
            try:
                cands = want.get(f.stat().st_size) if f.is_file() else None
            except OSError:
                continue
            if not cands or all(c["name"] in found for c in cands):
                continue
            h = _sha(f)
            for c in cands:
                if c["name"] not in found and c["sha256"] == h:
                    found[c["name"]] = f
        if len(found) == sum(len(v) for v in want.values()):
            break
    return found


def _clean_renders(keep=None):
    if not RENDER_DIR.exists():
        return
    for d in RENDER_DIR.iterdir():
        if d.name != keep:
            shutil.rmtree(d, ignore_errors=True)


def _upload_result(base, key, rid, out, timings):
    size = out.stat().st_size
    with open(out, "rb") as f:
        req = urllib.request.Request(base.rstrip("/") + f"/api/render/{rid}/result", data=f, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": "flow-bridge/" + VERSION,
                                              "Content-Type": "video/mp4", "Content-Length": str(size),
                                              "X-Render-Timings": json.dumps(timings)})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())


RENDER_ACTIVE = {}


def render_one(base, key, folder, job):
    """Render one Studio job here. Always removes its temp folder; Stop in Studio kills ffmpeg within ~2 s."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    rid, t0 = job["rid"], time.time()
    wd = RENDER_DIR / rid
    stop = threading.Event(); state = {"pct": 1, "msg": "Starting on your Mac"}
    RENDER_ACTIVE[rid] = stop

    def beat():
        while not stop.is_set():
            try:
                r = json.loads(_api(base, key, f"/api/render/{rid}/progress", state, timeout=20))
                if r.get("cancel"):
                    stop.set(); break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):
                    stop.set(); break
            except Exception:
                pass
            stop.wait(2)
    try:
        _clean_renders(keep=None)
        wd.mkdir(parents=True, exist_ok=True)
        ff = _ffmpeg()
        if not ff:
            raise RuntimeError("ffmpeg is not installed (run the one-line installer again)")
        tot = sum(i["size"] for i in job["inputs"])
        if tot > RENDER_DISK_CAP // 2 or shutil.disk_usage(str(RENDER_DIR)).free < tot * 3 + (300 << 20):
            raise RuntimeError("not enough free disk space for this render")
        done = threading.Event()
        threading.Thread(target=beat, daemon=True).start()
        eng = _engine(base, key, job.get("engine", ""))
        t_dl = time.time()
        local = _local_copies(folder, job.get("hint", ""), job["inputs"])

        def fetch(i):
            if stop.is_set():
                return
            dst = wd / i["name"]; dst.parent.mkdir(parents=True, exist_ok=True)
            if i["name"] in local:
                try:
                    os.link(local[i["name"]], dst)
                except OSError:
                    os.symlink(local[i["name"]], dst)
                return
            req = urllib.request.Request(base.rstrip("/") + f"/api/render/{rid}/file/{urllib.parse.quote(i['name'])}",
                                         headers={"Authorization": f"Bearer {key}", "User-Agent": "flow-bridge/" + VERSION})
            with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        state.update(pct=3, msg="Fetching inputs")
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(fetch, job["inputs"]))
        if stop.is_set():
            raise eng.Cancelled()
        t_dl = time.time() - t_dl
        mac = sys.platform == "darwin"
        cores = os.cpu_count() or 4

        def prog(pct, msg=""):
            state.update(pct=int(pct), msg=msg or state["msg"])
        out = wd / "out.mp4"
        enc = os.environ.get("FLOW_RENDER_ENCODER") or ("h264_videotoolbox" if mac else "libx264")
        r = eng.render(job["spec"], str(wd), str(out), ffmpeg=ff, encoder=enc,
                       workers=max(1, min(4, cores // 2)), threads="2", final_threads="0",
                       cancel=stop.is_set, progress=prog, log=lambda m: print(m, flush=True))
        r.update(fetch_s=round(t_dl, 1), reused=len(local), downloaded=len(job["inputs"]) - len(local))
        state.update(pct=99, msg="Uploading the video")
        t_up = time.time()
        _upload_result(base, key, rid, out, dict(r, upload_s=0))
        r["upload_s"] = round(time.time() - t_up, 1)
        print(f"[{time.strftime('%H:%M')}] rendered job {job['job'][:8]} on this computer in {time.time() - t0:.0f}s "
              f"(cut {r['cut_s']}s, final {r['final_s']}s, {r['encoder']}, {r['reused']} Flow files reused)", flush=True)
    except Exception as e:
        cancelled = type(e).__name__ == "Cancelled" or stop.is_set()
        print(f"Render {job['job'][:8]} {'stopped' if cancelled else 'failed: ' + str(e)[-200:]}", flush=True)
        if not cancelled:
            try:
                _api(base, key, f"/api/render/{rid}/fail", {"error": f"{type(e).__name__}: {str(e)[-300:]}"}, timeout=20)
            except Exception:
                pass
    finally:
        stop.set()
        RENDER_ACTIVE.pop(rid, None)
        shutil.rmtree(wd, ignore_errors=True)


def render_loop(base, key, folder):
    """Long-poll Studio for renders: a waiting job is claimed within a second, not on the next 20 s ping."""
    if os.environ.get("FLOW_RENDER", "1") == "0":
        return
    _clean_renders()
    if not _ffmpeg():
        _fetch_ffmpeg()
    bad = 0
    while True:
        if not _ffmpeg():
            time.sleep(1800); _fetch_ffmpeg(); continue
        try:
            job = json.loads(_api(base, key, "/api/render/next?wait=25", {}, timeout=60))
            bad = 0
        except urllib.error.HTTPError as e:
            bad += 1; time.sleep(min(300, 15 * bad)); continue  # 404 = Studio without Mac rendering yet
        except Exception:
            bad += 1; time.sleep(min(120, 5 * bad)); continue
        if job.get("rid"):
            render_one(base, key, folder, job)


def connect(base, key, folder, use_drive, cap, bg=False):
    """Stay online: heartbeat every 20 s, pick up new jobs (renders that chose the Flow engine) and fill them."""
    CTX.update(base=base, key=key)
    single_instance(bg)
    self_update()
    try:
        BASEF.parent.mkdir(mode=0o700, exist_ok=True); BASEF.write_text(base.rstrip("/"))
    except OSError:
        pass
    print(f"[{time.strftime('%Y-%m-%d %H:%M')}] flow-bridge {VERSION} connected to {base}. Studio uses Google Flow while this helper runs. (Ctrl+C to stop)", flush=True)
    last_upd = time.time()
    import threading
    threading.Thread(target=credits_loop, daemon=True).start()
    threading.Thread(target=render_loop, args=(base, key, folder), daemon=True).start()
    done_jobs = set()
    bad = 0
    while True:
        try:
            jobs = ping(base, key).get("jobs", [])
            bad = 0
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except Exception:
                body = {}
            if body.get("revoked"):  # only an explicit Unlink / pairing again elsewhere ends the pairing
                unpair("This computer was unlinked in Studio (or paired again on another computer). Pair again: Studio → Account → Connect my Google Flow.")
                sys.exit(0)
            bad += 1  # never give up or delete the key on its own: Studio may be restarting/redeploying
            wait = min(300, 20 * bad)
            print(f"(Studio did not accept this computer right now ({e.code}); keeping the pairing, retrying in {wait}s)", flush=True); time.sleep(wait); continue
        except Exception as e:
            bad += 1; wait = min(300, 20 * bad)
            print(f"(offline: {type(e).__name__}, retrying in {wait}s)", flush=True); time.sleep(wait); continue
        if not jobs and time.time() - last_upd > 6 * 3600:
            last_upd = time.time(); self_update()
        for job in jobs:
            if job["job"] in done_jobs:
                continue
            done_jobs.add(job["job"])
            out = folder / f"flow-{job['job'][:8]}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "prompts.txt").write_text("\n\n".join(s["prompt"] for s in job["scenes"]) + "\n")
            print(f"\nNew job {job['job'][:8]}: {len(job['scenes'])} scenes. Prompts saved to {out/'prompts.txt'}")
            print("  Models: " + ", ".join(f"{s['n']}:{s['type']}/{s['model']}" for s in job["scenes"]))
            for s in job["scenes"]:
                a = s.get("advanced") or {}
                if a:
                    print(f"  Scene {s['n']} settings: " + describe(a, s.get("type")))
            refs = job.get("references") or []
            for r in refs:  # character reference images -> Flow ingredients
                try:
                    dst = out / ("ref-" + re.sub(r"[^A-Za-z0-9_.-]", "_", r["name"]))
                    with urllib.request.urlopen(urllib.request.Request(base + r["url"], headers={"Authorization": "Bearer " + key}), timeout=60) as f:
                        dst.write_bytes(f.read())
                    print(f"  Character reference for Flow 'Ingredients': {dst}")
                except Exception as e:
                    print("  (could not fetch a character reference:", type(e).__name__, ")")
            if not use_drive:
                print("  In Flow: VEO Automation → pick the models above → Upload .txt → Run, saving into", out)
            watch(job, key, out, use_drive, cap, base, key)
        time.sleep(20)


def watch(job, token, folder, use_drive, cap, base=None, key=None):
    total = len(job["scenes"])
    print(f"Watching {folder} for {total} Flow files for job {job['job'][:8]} …")
    start, seen, done, last_ping = time.time() - 5, set(), sum(1 for s in job["scenes"] if s.get("filled")), 0
    if use_drive:
        import threading
        threading.Thread(target=drive, args=(job, folder, cap), daemon=True).start()
    while done < total and not job.get("stopped"):
        if base and time.time() - last_ping > 20:
            try:
                last_ping = time.time()
                if not any(x["job"] == job["job"] for x in ping(base, key).get("jobs", [])):
                    print(f"Job {job['job'][:8]} was stopped or finished in Studio."); return
            except Exception:
                pass
        new = sorted((p for p in folder.rglob("*") if p.suffix.lower() in MEDIA and p not in seen
                      and p.stat().st_mtime >= start and not p.name.startswith((".", "ref-")) and p.name != "prompts.txt"), key=lambda p: p.stat().st_mtime)
        for p in new:
            seen.add(p)
            if not stable(p):
                continue
            r = upload(job["upload_url"], job["job"], token, p, scene_from_name(p.name, total))
            if r.get("ok"):
                done = r["done"]; print(f"✓ {p.name} → scene {r['scene']}  ({done}/{total})")
            else:
                print(f"✗ {p.name}: {r.get('error')}")
                if r.get("code") == 403:
                    sys.exit("Token rejected. Copy it again from 'Send to Flow'.")
                if r.get("code") == 429:
                    print("Studio's daily Flow limit (50) is reached; the rest can be uploaded tomorrow."); return
                if r.get("code") == 409:
                    done = total
        time.sleep(3)
    print("All scenes filled." + ("" if base else " Go back to Studio and press ▶ Render now."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", help="a job .json downloaded from 'Send to Flow' (one-off mode)")
    ap.add_argument("--connect", metavar="STUDIO_URL", help="stay connected: Studio sends new jobs here automatically")
    ap.add_argument("--folder", default=str(Path.home() / "Downloads"), help="folder Flow / VEO Automation saves into")
    ap.add_argument("--token", default=os.environ.get("FLOW_TOKEN", ""), help="job token or bridge key (else asked, never saved)")
    ap.add_argument("--drive", action="store_true", default=True, help="drive Flow automatically with gflow-cli (default on)")
    ap.add_argument("--manual", action="store_true", help="never drive Flow; only watch the folder for files you make yourself")
    ap.add_argument("--signin", action="store_true", help="one-time: open Chrome to sign in to Google Flow (your own account)")
    ap.add_argument("--pair", nargs="?", const="ask", metavar="CODE", help="pair with the code from Studio → Account → Connect my Google Flow")
    ap.add_argument("--account", default="", help="label for the Google account signed into Flow (shown on your Account page)")
    ap.add_argument("--pair-only", action="store_true", help="pair, save the key, and exit (used by the one-click file)")
    ap.add_argument("--autostart", choices=["on", "off"], help="run the helper in the background at login (and restart it if it stops)")
    ap.add_argument("--status", action="store_true", help="show pairing, auto-start and connection status")
    ap.add_argument("--unpair", action="store_true", help="forget this computer's pairing and remove auto-start")
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pace", type=int, help="seconds between Flow generations (min 10; default: your Studio setting, else ~15)")
    ap.add_argument("--recover", action="store_true", help="re-download clips that Flow generated but that failed to download (no credits)")
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("--cap", type=int, default=50, help="daily cap for --drive (default 50, same as the Studio limit)")
    a = ap.parse_args()
    if a.pace:
        os.environ["FLOW_PACE"] = f"{max(10, a.pace)},{max(10, a.pace) + 3}"
    if gflow_bin():
        patch_gflow()
    if a.recover:
        g = gflow_bin()
        if not g:
            sys.exit("gflow-cli is not installed")
        left = recover_pending(g)
        print("All pending downloads recovered." if not left else f"{len(left)} download(s) still pending; try again later.")
        return
    if a.manual:
        a.drive = False
    if a.signin:
        g = gflow_bin()
        if not g:
            sys.exit("gflow-cli is missing. Re-run the installer.")
        print("A Chrome window opens. Sign in to YOUR Google account, then click 'Sign in to Flow' if asked. Close it when Flow shows your projects.")
        sys.exit(subprocess.run([g, "auth", "login", "--browser", "chrome"]).returncode)
    if a.unpair:
        return unpair()
    if a.autostart:
        if a.autostart == "on" and not KEYF.exists():
            sys.exit("Pair first: flow-bridge --connect https://moneyprinter-studio.onrender.com --pair")
        return autostart(a.autostart == "on", a.connect)
    if a.status:
        base = a.connect or (BASEF.read_text().strip() if BASEF.exists() else DEFAULT_BASE)
        print(f"flow-bridge {VERSION}\nStudio: {base}\nPaired: {'yes' if KEYF.exists() else 'no'}\nAuto-start: {autostart_state()}")
        if KEYF.exists():
            try:
                ping(base, KEYF.read_text().strip()); print("Studio connection: OK (this computer is recognised)")
            except urllib.error.HTTPError as e:
                print("Studio connection:", "UNLINKED, pair again" if b"revoked" in (e.read() or b"") else f"not accepted right now ({e.code}); the helper keeps retrying")
            except Exception as e:
                print("Studio connection: offline (", type(e).__name__, ")")
        return
    if not a.job and not a.connect:
        ap.error("use --connect https://moneyprinter-studio.onrender.com  or  --job file.json")
    folder = Path(a.folder).expanduser(); folder.mkdir(parents=True, exist_ok=True)
    if a.connect:
        global LABEL
        LABEL = (a.account or LABEL)[:80]
        if a.pair:
            code = a.pair if a.pair != "ask" else input("Pairing code from your Account page (e.g. ABCD-2345): ").strip()
            key = pair(a.connect, code, LABEL)
            import platform
            if platform.system() == "Darwin" or os.name == "nt":
                try:  # keep the connection on permanently: background helper at login, restarted if it stops
                    autostart(True, a.connect)
                    if not a.pair_only:
                        print("The background helper takes over when you close this window.")
                except Exception as e:
                    print("(could not turn on auto-start:", type(e).__name__, "; run: flow-bridge --autostart on)")
            if a.pair_only:
                return
        else:
            key = a.token or (KEYF.read_text().strip() if KEYF.exists() else "") or ("" if a.background else getpass.getpass("Paste your bridge key from Studio (hidden), or re-run with --pair: ").strip())
        if not key:
            sys.exit("Not paired yet: flow-bridge --connect https://moneyprinter-studio.onrender.com --pair")
        if gflow_bin() and _pending():
            try:
                recover_pending(gflow_bin())
            except Exception as e:
                print(f"[auto] pending downloads: {type(e).__name__}", flush=True)
        return connect(a.connect, key, folder, a.drive, a.cap, a.background)
    job = json.loads(Path(a.job).expanduser().read_text())
    token = a.token or getpass.getpass("Paste the upload token from Studio (hidden): ").strip()
    watch(job, token, folder, a.drive, a.cap)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
