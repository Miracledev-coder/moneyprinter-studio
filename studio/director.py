"""AI director (free): Gemini writes + self-critiques the script, plans every shot,
scores real stock thumbnails with vision in ONE call, and cuts clips to the voice timing."""
import base64, hashlib, json, math, os, re, subprocess, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0"}
CACHE = Path(os.environ.get("DIRECTOR_CACHE", "/tmp/director_cache")); CACHE.mkdir(parents=True, exist_ok=True)
BANNED = "success, business, growth, concept, idea, opportunity, money, future, technology, abstract, background"


def gem(prompt, images=(), temp=0.7, timeout=90):
    """Gemini JSON call (free tier). images: list of (label, jpeg bytes). Cached by content hash."""
    key = os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("DIRECTOR_MODEL") or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    h = hashlib.sha256((model + prompt + "".join(hashlib.md5(b).hexdigest() for _, b in images)).encode()).hexdigest()[:24]
    cf = CACHE / f"g_{h}.json"
    if cf.exists():
        return json.loads(cf.read_text())
    parts = [{"text": prompt}]
    for label, b in images:
        parts += [{"text": label}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}}]
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": temp}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    last = None
    for a in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            txt = d["candidates"][0]["content"]["parts"][0]["text"]
            out = json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()).strip())
            cf.write_text(json.dumps(out))
            return out
        except urllib.error.HTTPError as e:
            last = f"Gemini error {e.code}"
        except Exception as e:
            last = f"Gemini error {type(e).__name__}"
        import time; time.sleep(6 * (a + 1))
    raise RuntimeError(last)


RULES = """Short-form rules (TikTok/Reels/Shorts):
- Line 1 is the HOOK, spoken in under 2 seconds (max 10 words): a surprising specific claim, number or contradiction. Never start with "Did you know", "In today's world", "Have you ever", "Let's talk about".
- Open a curiosity gap in the first 2 lines and pay it off near the end.
- Every line must carry a concrete, specific detail (a place, number, object, action, named example). Delete any line that could fit any topic.
- No filler, no clichés ("game changer", "unlock", "in conclusion", "the possibilities are endless").
- Short spoken sentences (6-16 words), one idea each, plain words, no emojis, no hashtags, no stage directions.
- End with a payoff line then a short, specific CTA (e.g. follow for part 2 / comment X).
- Never invent precise statistics; use honest wording ("thousands", "hours a day") unless the fact is common knowledge."""


def write_script(topic, secs, given=""):
    lo, hi = round(secs * 2.3 * 0.9), round(secs * 2.3 * 1.05)
    src = (f"Rewrite this script into a stronger short video, keeping its facts and intent:\n{given}" if given
           else f"Topic: {topic}")
    d = gem(f"""You are a top short-form scriptwriter. {src}
Length: {lo}-{hi} words total (~{secs}s spoken).
{RULES}
Return JSON {{"script": "the full narration, one sentence per line"}}""", temp=0.9)
    draft = str(d.get("script", "")).strip()
    r = gem(f"""You are a harsh short-form video editor. Critique this {secs}s script against the rules, then rewrite it to fix EVERY problem.
{RULES}
Length must stay {lo}-{hi} words.
SCRIPT:
{draft}
Return JSON {{"critique": ["problem 1", ...], "hook_score_before": 0-10, "script": "the revised narration, one sentence per line", "hook_score_after": 0-10}}""", temp=0.6)
    final = str(r.get("script") or draft).strip()
    if len(final.split()) < lo * 0.92:  # models undershoot length: one targeted expansion so the video hits the chosen length
        e = gem(f"""This script is {len(final.split())} words but must be {lo}-{hi} words. Add concrete detail lines (numbers, places,
what you would see) in the middle; keep the hook first line and the CTA last line unchanged. {RULES}
SCRIPT:
{final}
Return JSON {{"script": "the full narration, one sentence per line"}}""", temp=0.5)
        if lo * 0.85 <= len(str(e.get("script", "")).split()) <= hi * 1.1:
            final = str(e["script"]).strip()
    return {"draft": draft, "critique": r.get("critique") or [], "script": final,
            "hook_before": r.get("hook_score_before"), "hook_after": r.get("hook_score_after")}


def sentences(text):
    s = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text) if x.strip()]
    out = []
    for x in s:  # merge very short fragments so no shot is under ~1.2s
        if out and len(x.split()) < 4:
            out[-1] += " " + x
        else:
            out.append(x)
    return out[:24]


def plan_shots(lines, topic):
    d = gem(f"""You are a film director planning stock footage for a short video about: {topic}.
For EACH numbered narration line, describe one concrete shot a camera could film, and give 4 stock-footage search queries
(2-4 words each, literal and visual: objects, places, people doing actions; e.g. "traffic jam aerial", "street vendor selling water").
Never use abstract words alone ({BANNED}). Avoid posed corporate stock (handshakes, people pointing at screens, thumbs up).
If the topic names a country or city, the footage must look like THAT place: put the place or region in at least 2 of the 4 queries
(e.g. "lagos traffic", "nigeria street market", "african city bus"), and never pick scenes with foreign-language signs or obviously different countries.
Also choose ONE overall colour look for the whole video, and give 4 "broll" queries for safe, on-location establishing footage
of the setting (used whenever a specific shot has no good match), e.g. "lagos street", "african city traffic".
Lines:
""" + "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines)) + """
Return JSON {"look": "warm|cool|neutral|moody", "broll": ["..",".."], "shots": [{"visual": "...", "queries": ["..",".."], "shot": "wide|medium|close-up|aerial|pov", "mood": "..."}]} with exactly one shot per line.""", temp=0.5)
    shots = (d.get("shots") or [])[:len(lines)]
    while len(shots) < len(lines):
        w = sorted(re.findall(r"[A-Za-z]{4,}", lines[len(shots)]), key=len, reverse=True)[:2]
        shots.append({"visual": lines[len(shots)], "queries": [" ".join(w) or "city street"], "shot": "medium", "mood": ""})
    for s in shots:
        s["queries"] = [re.sub(r"[^A-Za-z0-9 \-]", "", str(q))[:40].strip() for q in (s.get("queries") or [])][:5]
    broll = [re.sub(r"[^A-Za-z0-9 \-]", "", str(q))[:40].strip() for q in (d.get("broll") or [])][:4] or ["city street", "busy road"]
    shots.append({"visual": "on-location establishing footage of the setting: " + topic, "queries": broll, "shot": "wide", "mood": "", "broll": True})
    return (d.get("look") or "neutral"), shots


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def pixabay(q, portrait):
    key = os.environ.get("PIXABAY_API_KEY", "")
    if not key or not q:
        return []
    cf = CACHE / ("p_" + hashlib.md5(q.encode()).hexdigest() + ".json")
    try:
        hits = json.loads(cf.read_text()) if cf.exists() else None
        if hits is None:
            hits = json.loads(_get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
                {"key": key, "q": q[:80], "per_page": 12, "safesearch": "true"}), 15)).get("hits", [])
            cf.write_text(json.dumps(hits))
    except Exception:
        return []
    out = []
    for h in hits:
        v = h.get("videos") or {}
        f = next((v[n] for n in ("medium", "small", "large") if (v.get(n) or {}).get("url") and int(v[n].get("width") or 0) >= 960), None) \
            or next((v[n] for n in ("small", "medium") if (v.get(n) or {}).get("url")), None)
        if not f:
            continue
        w, hh = int(f.get("width") or 1), int(f.get("height") or 1)
        out.append({"id": f"px{h['id']}", "url": f["url"], "thumb": (v.get("small") or v.get("tiny") or {}).get("thumbnail") or f.get("thumbnail", ""),
                    "dur": float(h.get("duration") or 0), "w": w, "h": hh, "fit": (hh > w) == portrait, "q": q, "tags": h.get("tags", "")})
    return out


def pexels(q, portrait):
    key = os.environ.get("PEXELS_API_KEY", "")
    if not key or not q:
        return []
    try:
        req = urllib.request.Request("https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
            {"query": q, "per_page": 8, "orientation": "portrait" if portrait else "landscape"}), headers=dict(UA, Authorization=key))
        with urllib.request.urlopen(req, timeout=15) as r:
            vids = json.loads(r.read()).get("videos", [])
    except Exception:
        return []
    out = []
    for v in vids:
        fs = sorted([f for f in v.get("video_files", []) if f.get("link") and (f.get("width") or 0) >= 720], key=lambda f: f.get("width") or 0)
        if not fs:
            continue
        f = fs[0]
        out.append({"id": f"pe{v['id']}", "url": f["link"], "thumb": v.get("image", ""), "dur": float(v.get("duration") or 0),
                    "w": f["width"], "h": f["height"], "fit": (f["height"] > f["width"]) == portrait, "q": q, "tags": ""})
    return out


def _thumb(url):
    cf = CACHE / ("t_" + hashlib.md5(url.encode()).hexdigest() + ".jpg")
    if not cf.exists():
        raw = _get(url, 15)
        tmp = cf.with_suffix(".src")
        tmp.write_bytes(raw)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp), "-vf", "scale=256:-2", "-q:v", "6", str(cf)], timeout=30)
        tmp.unlink(missing_ok=True)
    return cf.read_bytes()


def candidates(shots, portrait, per_shot=4):
    def one(s):
        seen, pool = set(), []
        for q in s["queries"]:
            for c in pixabay(q, portrait) + pexels(q, portrait):
                if c["id"] not in seen:
                    seen.add(c["id"]); pool.append(c)
        # pre-rank cheaply: matching orientation, HD, long enough, earlier (more literal) query
        qi = {q: i for i, q in enumerate(s["queries"])}
        pool.sort(key=lambda c: (not c["fit"], min(c["w"], c["h"]) < 720, qi.get(c["q"], 9)))
        return pool[:per_shot * 2]
    with ThreadPoolExecutor(4) as ex:
        return list(ex.map(one, shots))


def score_and_pick(lines, shots, pools, look, per_shot=4):
    lines = list(lines) + (["(B-roll: general footage of the setting, no specific line)"] if shots and shots[-1].get("broll") else [])
    """Fetch thumbnails, ONE Gemini vision call scores every candidate, then greedy unique pick."""
    imgs, keys = [], []
    for i, pool in enumerate(pools):
        n = 0
        for j, c in enumerate(pool):
            if n >= per_shot:
                break
            try:
                imgs.append((f"[S{i+1}C{j+1}]", _thumb(c["thumb"]))); keys.append((i, j)); n += 1
            except Exception:
                pass
    scores = {}
    if imgs:
        listing = "\n".join(f"S{i+1}: line=\"{l}\" | intended shot: {s.get('visual','')}" for i, (l, s) in enumerate(zip(lines, shots)))
        try:
            d = gem(f"""You are a picky video editor. Each image below is a thumbnail of a stock clip, labelled [S<line>C<candidate>].
Score how well each clip visually matches ITS line's narration and intended shot (0-10). Penalise: unrelated subject, wrong country/setting
(e.g. Asian/European streets, signs in other languages, mountains or empty countryside when the topic is an African megacity), visible watermark or text overlay, cheesy posed corporate stock, low quality, cartoon/3D render.
Lines:
{listing}
Overall look wanted: {look}.
Return JSON {{"scores": [{{"id": "S1C1", "score": 0-10, "watermark": false, "cheesy": false, "why": "few words"}}]}} for every image.""", imgs, temp=0.2, timeout=120)
            for r in d.get("scores") or []:
                m = re.match(r"S(\d+)C(\d+)", str(r.get("id", "")))
                if m:
                    sc = float(r.get("score") or 0) - (6 if r.get("watermark") else 0) - (3 if r.get("cheesy") else 0)
                    scores[(int(m[1]) - 1, int(m[2]) - 1)] = (sc, str(r.get("why", ""))[:80])
            print(f"director: vision scored {len(scores)}/{len(imgs)}", flush=True)
        except Exception:
            pass
    def val(i, j, c):
        return scores.get((i, j), (2.0 - j * 0.2, "unscored"))[0] + (1.0 if c["fit"] else 0) + (0.5 if min(c["w"], c["h"]) >= 720 else 0)
    has_broll = bool(shots) and shots[-1].get("broll")
    n = len(pools) - (1 if has_broll else 0)
    bi = len(pools) - 1
    used, picks = set(), []
    for i in range(n):
        best, bs = None, -99
        for j, c in enumerate(pools[i]):
            if c["id"] not in used and (v := val(i, j, c)) > bs:
                best, bs = dict(c, score=round(v, 1), why=scores.get((i, j), (0, "unscored"))[1]), v
        raw = scores.get((i, [x["id"] for x in pools[i]].index(best["id"])), (99,))[0] if best else -99
        if has_broll and raw < 5:  # weak literal match: use on-location B-roll instead of an off-topic clip
            for j, c in enumerate(pools[bi]):
                if c["id"] not in used and (v := val(bi, j, c)) > bs and scores.get((bi, j), (0,))[0] >= 6:
                    best, bs = dict(c, score=round(v, 1), why="b-roll: " + scores.get((bi, j), (0, ""))[1]), v
        if best:
            used.add(best["id"])
        picks.append(best)
    return picks, len(imgs)


def word_times(text, voice, rate, mp3):
    """Edge TTS once to get word boundaries; returns list of (start_s, end_s, word) and audio duration."""
    from app.services import voice as vs
    sm = vs.tts(text=text, voice_name=voice, voice_rate=rate, voice_file=mp3)
    cues = getattr(sm, "cues", None) or []
    out = []
    for c in cues:
        st, en = c.start.total_seconds(), c.end.total_seconds()
        out.append((st, en, str(c.content)))
    if not out:  # legacy SubMaker
        for (a, b), w in zip(getattr(sm, "offset", []), getattr(sm, "subs", [])):
            out.append((a / 1e7, b / 1e7, w))
    return out, vs.get_audio_duration(sm) if hasattr(vs, "get_audio_duration") else (out[-1][1] if out else 0)


def sentence_durations(lines, words, total):
    """Cut points = start time of each line's first word, from the voice's word timestamps."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    toks = [norm(w) for _, _, w in words]
    starts, k = [], 0
    for l in lines:
        first = next((norm(w) for w in l.split() if norm(w)), "")
        j = k
        while j < len(toks) and toks[j] != first:
            j += 1
        if j >= len(toks):
            j = k
        starts.append(words[j][0] if j < len(words) else (starts[-1] + 2 if starts else 0))
        k = j + max(1, len([w for w in l.split() if norm(w)]))
    starts[0] = 0.0
    total = max(total, (words[-1][1] if words else 0)) + 0.6
    return [max(1.0, (starts[i + 1] if i + 1 < len(starts) else total) - starts[i]) for i in range(len(starts))]


GRADE = {"warm": "colorbalance=rs=0.04:gs=0.0:bs=-0.05:rm=0.03:bm=-0.03",
         "cool": "colorbalance=rs=-0.04:bs=0.05:bm=0.03",
         "moody": "colorbalance=rs=0.02:bs=0.03,eq=saturation=0.85:contrast=1.08:brightness=-0.02",
         "neutral": "null"}


def cut(src, dst, dur, W, H, look, still=False, zoom=True):
    """Crop-to-fill, light grade (same for every clip), subtle push-in on static/slow shots, exact duration."""
    grade = f"eq=contrast=1.06:saturation=1.05,{GRADE.get(look, 'null')}"
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    frames = max(1, int(dur * 30))
    if still:
        vf = f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},zoompan=z='1+0.0015*on':d={frames}:s={W}x{H}:fps=30,{grade},format=yuv420p"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", src, "-t", f"{dur:.2f}", "-vf", vf]
    else:
        _, _, sd = probe(src)
        off = 0.4 if sd > dur + 1.0 else 0
        speed = min(1.0, sd / dur) if 0 < sd < dur else 1.0  # slightly slow a short clip instead of looping, down to 0.75x
        loop = ["-stream_loop", "-1"] if speed < 0.75 else []
        pts = f"setpts={1/speed:.3f}*PTS," if 0.75 <= speed < 1.0 else ""
        z = f"scale={int(W*1.06)}:{int(H*1.06)},crop={W}:{H}:x='(iw-ow)/2*(1-t/{dur:.2f})':y='(ih-oh)/2'," if zoom else ""
        vf = f"{pts}{fit},{z}fps=30,{grade},format=yuv420p"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", *loop, "-ss", f"{off}", "-i", src, "-t", f"{dur:.2f}", "-vf", vf]
    cmd += ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "1", dst]
    subprocess.run(cmd, timeout=300, check=True)


def probe(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, timeout=30)
    e = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", e)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", e)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    return (int(m[1]), int(m[2]), dur) if m else (0, 0, dur)


def direct(jid, topic, script, secs, voice, rate, aspect, workdir, log=print):
    """Full director pass. Returns dict(script, clips=[paths], clip_max, report)."""
    W, H = (1280, 720) if aspect == "16:9" else (720, 1280)  # same as the final render: less RAM, no double scaling
    workdir = Path(workdir); workdir.mkdir(parents=True, exist_ok=True)
    log("director: writing + critiquing script")
    sc = write_script(topic or script[:120], secs, given=script if script and not topic else "")
    text = sc["script"]
    lines = sentences(text)
    text = " ".join(lines)
    log(f"director: planning {len(lines)} shots")
    look, shots = plan_shots(lines, topic or text[:120])
    pools = candidates(shots, aspect != "16:9")
    picks, nimg = score_and_pick(lines, shots, pools, look)
    log(f"director: scored {nimg} candidates in one vision call")
    words, total = word_times(text, voice, rate, str(workdir / f"{jid[:8]}_probe.mp3"))
    durs = sentence_durations(lines, words, total)
    clips, rep = [], []
    for i, (l, s, p, d) in enumerate(zip(lines, shots, picks, durs)):
        if not p:
            continue
        raw = workdir / f"{jid[:8]}_raw{i:02d}.mp4"
        try:
            raw.write_bytes(_get(p["url"], 90))
            out = workdir / f"{jid[:8]}_{i:02d}.mp4"
            cut(str(raw), str(out), d, W, H, look, zoom=(i % 2 == 0))
            clips.append(str(out))
        except Exception as e:
            log(f"director: clip {i} failed {type(e).__name__}")
        finally:
            raw.unlink(missing_ok=True)
        rep.append({"line": l, "visual": s.get("visual"), "queries": s.get("queries"), "pick": p.get("id"), "q": p.get("q"),
                    "score": p.get("score"), "why": p.get("why"), "dur": round(d, 2)})
    print("director report:", json.dumps({"look": look, "critique": sc["critique"], "hook": [sc["hook_before"], sc["hook_after"]], "shots": rep})[:6000], flush=True)
    return {"script": text, "clips": clips, "clip_max": math.ceil(max(durs)) + 1, "look": look, "report": rep,
            "draft": sc["draft"], "critique": sc["critique"], "hook": [sc["hook_before"], sc["hook_after"]]}
