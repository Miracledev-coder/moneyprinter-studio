#!/usr/bin/env python3
"""flow-bridge: send Google Flow clips from your Mac straight into MoneyPrinter Studio scenes.

Default (safe) route: you run the prompts in Flow with the free VEO Automation extension, it downloads
the clips, and this helper uploads each new file to the next empty scene of your Studio job.

  python3 flow_bridge.py --job ~/Downloads/flow-xxxx.json          # asks for the upload token once
  python3 flow_bridge.py --job flow-xxxx.json --folder ~/Downloads/my-project

Experimental: --drive uses gflow-cli (https://github.com/ffroliva/gflow-cli, unofficial) to drive Flow
with your own signed-in Chrome, slowly (75-120 s between prompts) with a daily cap of 50. Use at your own risk.
No password is ever asked for or stored. Python standard library only.
"""
import argparse, getpass, json, mimetypes, os, random, re, shutil, subprocess, sys, time, urllib.request, uuid
from pathlib import Path

MEDIA = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
STATE = Path.home() / ".flow-bridge"
# Flow model-picker names -> gflow-cli --model aliases, checked against `gflow models --json` (gflow-cli 0.92.1, Oct 2026).
# Override with GFLOW_MODEL_MAP='{"Nano Banana Pro":"nano-pro"}' if a later gflow renames them.
GFLOW_MODELS = {"Nano Banana Pro": "nano-pro", "Nano Banana 2": "nano2", "Nano Banana 2 Lite": "nano2-lite", "Imagen 4": "image4",
                "Veo 3.1 - Lite": "veo-lite", "Veo 3.1 - Fast": "veo-fast", "Veo 3.1 - Quality": "veo-quality", "Omni Flash": "omni-flash"}
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


def gflow_bin():
    for c in (shutil.which("gflow"), str(Path.home() / ".local/bin/gflow")):
        if c and Path(c).exists():
            return c
    return None


DRIFT = 23  # gflow-cli exit code: Flow's page changed (selector drift) -> stop driving, fall back to manual


def drive(job, folder, cap, pace=(75, 120)):
    """Route A/B: gflow-cli drives labs.google/flow in a real, headed Chrome with your own signed-in profile.
    One scene at a time, 75-120 s apart, daily cap. Each file is saved as NN_scene.ext so it lands in the right scene."""
    gflow = gflow_bin()
    if not gflow:
        print("[auto] gflow-cli is not installed; using the manual route (VEO Automation / drag-and-drop)."); return False
    if subprocess.run([gflow, "auth", "status"], capture_output=True).returncode:
        print("[auto] Flow is not signed in on this computer. Run:  flow-bridge --signin   (once), then start again."); return False
    STATE.mkdir(exist_ok=True)
    cf = STATE / f"count-{time.strftime('%Y-%m-%d')}.txt"
    used = int(cf.read_text() or 0) if cf.exists() else 0
    refs = sorted(str(p) for p in Path(folder).glob("ref-*"))
    for sc in job["scenes"]:
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
        if alias:
            cmd += ["--model", alias]
        print(f"[auto] scene {sc['n']}: {kind} · {model or 'Flow default'}{' · ' + str(len(my)) + ' character ref(s)' if my else ''} …", flush=True)
        r = subprocess.run(cmd, cwd=folder)
        if r.returncode == DRIFT:
            print("[auto] Flow's page changed and gflow-cli can't find its buttons (exit 23). Stopping automation;"
                  " finish the remaining scenes by hand (prompts.txt) or update:  uv tool upgrade gflow-cli"); return False
        if r.returncode and r.returncode != DRIFT and agent and not out.exists():
            print("[auto] Flow Agent instruction was not accepted on this account; retrying without it")
            i = cmd.index("-i"); cmd = cmd[:i] + cmd[i + 2:]
            r = subprocess.run(cmd, cwd=folder)
        if r.returncode and alias:
            print(f"[auto] '{alias}' was not accepted; retrying with Flow's default model")
            r = subprocess.run(cmd[:-2], cwd=folder)
        if not out.exists() and kind == "image" and my:  # i2i wrote into tmp: move it in with the scene number
            new = sorted((STATE / "tmp").glob("*.*"), key=lambda p: p.stat().st_mtime)
            if new:
                shutil.move(str(new[-1]), str(out.with_suffix(new[-1].suffix)))
        used += 1; cf.write_text(str(used))
        if r.returncode:
            print(f"[auto] scene {sc['n']} failed (exit {r.returncode}); it stays open for a manual upload.")
        wait = random.randint(*pace)
        print(f"[auto] pausing {wait}s so Flow sees a human pace"); time.sleep(wait)
    return True


KEYF = Path.home() / ".flow-bridge" / "key"
LABEL = os.environ.get("FLOW_ACCOUNT", "")


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
    KEYF.write_text(d["key"]); KEYF.chmod(0o600)  # stays on this computer only; never printed
    print(f"Paired with Studio account '{d['user']}'. Keep Google Flow signed in to YOUR account in Chrome.")
    return d["key"]


CREDITS = {"n": None, "at": 0}


def read_credits():
    """Flow credit balance via `gflow credits user --json` (reads the number in Flow's account menu; spends nothing)."""
    g = gflow_bin()
    if not g:
        return
    try:
        out = subprocess.run([g, "credits", "user", "--json"], capture_output=True, text=True, timeout=180).stdout
        d = json.loads(out[out.find("{"):] or "{}")
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
    body = json.dumps({"credits": CREDITS["n"]} if CREDITS["n"] is not None else {}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/api/flow/ping", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "flow-bridge/1.0",
                                          **({"X-Flow-Account": LABEL} if LABEL else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def connect(base, key, folder, use_drive, cap):
    """Stay online: heartbeat every 20 s, pick up new jobs (renders that chose the Flow engine) and fill them."""
    print(f"Connected to {base}. Studio now uses Google Flow while this window stays open. (Ctrl+C to stop)")
    import threading
    threading.Thread(target=credits_loop, daemon=True).start()
    done_jobs = set()
    while True:
        try:
            jobs = ping(base, key).get("jobs", [])
        except urllib.error.HTTPError as e:
            if e.code == 403:
                KEYF.unlink(missing_ok=True)
                sys.exit("This computer was unlinked (or paired again elsewhere). Pair again: Studio → Account → Connect my Google Flow.")
            print(f"(server said {e.code}, retrying)"); time.sleep(30); continue
        except Exception as e:
            print(f"(offline: {type(e).__name__}, retrying)"); time.sleep(20); continue
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
    while done < total:
        if base and time.time() - last_ping > 20:
            try:
                ping(base, key); last_ping = time.time()
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
    ap.add_argument("--cap", type=int, default=50, help="daily cap for --drive (default 50, same as the Studio limit)")
    a = ap.parse_args()
    if a.manual:
        a.drive = False
    if a.signin:
        g = gflow_bin()
        if not g:
            sys.exit("gflow-cli is missing. Re-run the installer.")
        print("A Chrome window opens. Sign in to YOUR Google account, then click 'Sign in to Flow' if asked. Close it when Flow shows your projects.")
        sys.exit(subprocess.run([g, "auth", "login", "--browser", "chrome"]).returncode)
    if not a.job and not a.connect:
        ap.error("use --connect https://moneyprinter-studio.onrender.com  or  --job file.json")
    folder = Path(a.folder).expanduser(); folder.mkdir(parents=True, exist_ok=True)
    if a.connect:
        global LABEL
        LABEL = (a.account or LABEL)[:80]
        if a.pair:
            code = a.pair if a.pair != "ask" else input("Pairing code from your Account page (e.g. ABCD-2345): ").strip()
            key = pair(a.connect, code, LABEL)
            if a.pair_only:
                return
        else:
            key = a.token or (KEYF.read_text().strip() if KEYF.exists() else "") or getpass.getpass("Paste your bridge key from Studio (hidden), or re-run with --pair: ").strip()
        return connect(a.connect, key, folder, a.drive, a.cap)
    job = json.loads(Path(a.job).expanduser().read_text())
    token = a.token or getpass.getpass("Paste the upload token from Studio (hidden): ").strip()
    watch(job, token, folder, a.drive, a.cap)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
