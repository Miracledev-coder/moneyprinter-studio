"""Scene planning: per-sentence footage matching (Gemini keywords + Pixabay), uploads, hook check."""
import json, os, re, shutil, subprocess, urllib.parse, urllib.request, uuid
from pathlib import Path
from ideas import _gemini

UP_DIR = Path(os.environ.get("STUDIO_UPLOADS", "/tmp/studio_uploads"))
VID_EXT = {".mp4", ".mov", ".webm", ".m4v"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD = 60 * 1024 * 1024


def sentences(text):
    t = re.sub(r"\s+", " ", text or "").strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    return parts


def split_scenes(script, n_max=30):
    """Group sentences into scenes of roughly 6-20 words."""
    out, cur = [], ""
    for s in sentences(script):
        if cur and len((cur + " " + s).split()) > 20:
            out.append(cur); cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        out.append(cur)
    while len(out) > n_max:  # merge neighbours
        i = min(range(len(out) - 1), key=lambda i: len(out[i].split()) + len(out[i + 1].split()))
        out[i:i + 2] = [out[i] + " " + out[i + 1]]
    return out


def keywords(scenes):
    prompt = ("For each numbered scene of a short video script, give ONE concrete, visual stock-footage search query "
              "(2-3 plain English words, things a camera can film, no abstract words). Return JSON: "
              '{"k": ["query for scene 1", ...]} with exactly ' + str(len(scenes)) + " items.\n\n"
              + "\n".join(f"{i+1}. {s}" for i, s in enumerate(scenes)))
    try:
        d = _gemini(prompt, timeout=40)
        k = d.get("k") if isinstance(d, dict) else d
        k = [re.sub(r"[^A-Za-z0-9 \-]", "", str(x))[:40].strip() for x in (k or [])]
    except Exception:
        k = []
    while len(k) < len(scenes):  # fallback: longest words of the sentence
        s = scenes[len(k)]
        w = sorted(re.findall(r"[A-Za-z]{4,}", s), key=len, reverse=True)[:2]
        k.append(" ".join(w) or "city life")
    return k[:len(scenes)]


def pixabay(q, aspect="9:16", n=6):
    key = os.environ.get("PIXABAY_API_KEY", "")
    if not key:
        return []
    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
        {"key": key, "q": q[:80], "per_page": 20, "safesearch": "true"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
            hits = json.loads(r.read()).get("hits", [])
    except Exception:
        return []
    portrait = aspect != "16:9"
    out = []
    for h in hits:
        v = h.get("videos") or {}
        best = None
        for name in ("small", "medium", "tiny", "large"):
            f = v.get(name) or {}
            if f.get("url") and int(f.get("width") or 0) >= 540:
                best = f; break
        best = best or next((v[n2] for n2 in ("tiny", "small") if (v.get(n2) or {}).get("url")), None)
        if not best:
            continue
        w, hh = int(best.get("width") or 1), int(best.get("height") or 1)
        out.append({"id": h.get("id"), "url": best["url"], "thumb": (v.get("tiny") or {}).get("thumbnail") or best.get("thumbnail") or "",
                    "dur": h.get("duration"), "fit": (hh > w) == portrait})
    out.sort(key=lambda o: not o["fit"])
    return out[:n]


def plan(script, aspect):
    sc = split_scenes(script)
    ks = keywords(sc)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        opts = list(ex.map(lambda q: pixabay(q, aspect), ks))
    return [{"text": s, "q": k, "options": o, "pick": 0 if o else -1} for s, k, o in zip(sc, ks, opts)]


def probe(path):
    """(width, height, duration) via ffmpeg's banner (no ffprobe in the image)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, timeout=30)
    e = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", e)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", e)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    return (int(m[1]), int(m[2]), dur) if m else (0, 0, dur)


def save_upload(user, filename, fileobj):
    ext = Path(filename or "").suffix.lower()
    if ext not in VID_EXT | IMG_EXT:
        raise ValueError("Only MP4/MOV/WEBM videos or JPG/PNG/WEBP images.")
    d = UP_DIR / re.sub(r"[^A-Za-z0-9_-]", "", user)
    d.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    p = d / (uid + ext)
    size = 0
    with open(p, "wb") as f:
        while chunk := fileobj.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close(); p.unlink(missing_ok=True)
                raise ValueError("File too large (max 60 MB).")
            f.write(chunk)
    # verify it's real media
    w, _h, _d = probe(p)
    if not w:
        p.unlink(missing_ok=True)
        raise ValueError("That file doesn't look like a valid video or image.")
    # thumbnail
    th = d / (uid + ".thumb.jpg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(p), "-frames:v", "1", "-vf", "scale=240:-2", str(th)],
                   capture_output=True, timeout=30)
    # keep each user's uploads bounded (last 40 files)
    files = sorted((f for f in d.iterdir() if ".thumb" not in f.name), key=lambda f: f.stat().st_mtime)
    for f in files[:-40]:
        f.unlink(missing_ok=True); (d / (f.stem + ".thumb.jpg")).unlink(missing_ok=True)
    return {"id": uid + ext, "kind": "image" if ext in IMG_EXT else "video"}


def upload_path(user, uid):
    if not re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{2,5}", uid or ""):
        return None
    p = UP_DIR / re.sub(r"[^A-Za-z0-9_-]", "", user) / uid
    return p if p.exists() else None


def materialize(jid, user, scenes, dest_dir):
    """Download/copy each scene's chosen clip into dest_dir in order. Returns list of local paths."""
    dest_dir = Path(dest_dir); dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, s in enumerate(scenes[:30]):
        src = str(s.get("src") or "")
        if src.startswith("upload:"):
            p = upload_path(user, src[7:])
            if p:
                d = dest_dir / f"{jid[:8]}_{i:02d}{p.suffix}"
                shutil.copy(p, d); out.append(str(d))
        elif src.startswith("https://cdn.pixabay.com/") or src.startswith("https://player.vimeo.com/"):
            d = dest_dir / f"{jid[:8]}_{i:02d}.mp4"
            try:
                with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r, open(d, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                out.append(str(d))
            except Exception:
                pass
    return out


def hook(script):
    first = (sentences(script) or [script.strip()])[0][:300]
    prompt = ("You are a short-form video hook coach. Score this opening line of a TikTok/Reels/Shorts script from 1 to 10 "
              "for scroll-stopping power (curiosity, specificity, stakes, brevity). Then write 3 stronger alternative opening "
              "lines (max 14 words each) that keep the same topic and promise. Return JSON: "
              '{"score": n, "why": "one short sentence", "openers": ["...", "...", "..."]}\n\n'
              f"Opening line: {first}\n\nRest of script for context: {script[len(first):len(first)+600]}")
    d = _gemini(prompt, timeout=40)
    if isinstance(d, list):
        d = d[0] if d else {}
    return {"first": first, "score": max(1, min(int(d.get("score") or 5), 10)), "why": str(d.get("why") or "")[:200],
            "openers": [str(o).strip()[:160] for o in (d.get("openers") or [])][:3]}
