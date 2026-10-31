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


def write_script(topic, secs, given="", style=""):
    st = f"\nFORMAT (follow it, it overrides generic structure): {style[:600]}" if style else ""
    lo, hi = round(secs * 2.3 * 0.9), round(secs * 2.3 * 1.05)
    src = (f"Rewrite this script into a stronger short video, keeping its facts and intent:\n{given}" if given
           else f"Topic: {topic}")
    d = gem(f"""You are a top short-form scriptwriter. {src}
Length: {lo}-{hi} words total (~{secs}s spoken).
{RULES}{st}
Return JSON {{"script": "the full narration, one sentence per line"}}""", temp=0.9)
    draft = str(d.get("script", "")).strip()
    r = gem(f"""You are a harsh short-form video editor. Critique this {secs}s script against the rules, then rewrite it to fix EVERY problem.
{RULES}{st}
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
            hits = []
            for page in (1, 2):  # two pages, every video type (film + animation) to widen the pool
                r = json.loads(_get("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(
                    {"key": key, "q": q[:80], "per_page": 20, "page": page, "safesearch": "true", "video_type": "all"}), 15))
                hits += r.get("hits", [])
                if len(r.get("hits", [])) < 20 or int(r.get("totalHits") or 0) <= 20:
                    break
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


BOT_UA = {"User-Agent": "MoneyPrinterStudio/1.0 (https://github.com/Miracledev-coder/moneyprinter-studio)"}
FREE_LICENCES = re.compile(r"^(cc0|public domain|pd|cc by( \d\.\d)?|cc-by-\d\.\d)$", re.I)


def commons(q, portrait):
    """Wikimedia Commons video search (public API, no key). Only CC0 / public domain / CC BY clips are kept;
    CC BY credit is stored in 'credit' for the description. ShareAlike and NonCommercial clips are skipped."""
    if not q:
        return []
    cf = CACHE / ("c_" + hashlib.md5(q.encode()).hexdigest() + ".json")
    try:
        if cf.exists():
            pages = json.loads(cf.read_text())
        else:
            req = urllib.request.Request("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
                "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{q} filetype:video",
                "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
                "iiextmetadatafilter": "LicenseShortName|Artist", "iiurlwidth": 320}), headers=BOT_UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                pages = list((json.loads(r.read()).get("query") or {}).get("pages", {}).values())
            cf.write_text(json.dumps(pages))
    except Exception:
        return []
    out = []
    for p in pages:
        i = (p.get("imageinfo") or [{}])[0]
        md = i.get("extmetadata") or {}
        lic = str((md.get("LicenseShortName") or {}).get("value", "")).strip()
        if not FREE_LICENCES.match(lic) or not i.get("url") or not i.get("thumburl"):
            continue
        w, h, dur = int(i.get("width") or 0), int(i.get("height") or 0), float(i.get("duration") or 0)
        if min(w, h) < 480 or dur < 3:
            continue
        artist = re.sub(r"<[^>]+>", "", str((md.get("Artist") or {}).get("value", "")))[:60]
        out.append({"id": "wc" + str(p.get("pageid")), "url": i["url"], "thumb": i["thumburl"], "dur": dur, "w": w, "h": h,
                    "fit": (h > w) == portrait, "q": q, "tags": p.get("title", ""),
                    "credit": f"{p.get('title', '')} by {artist} ({lic}), Wikimedia Commons"})
    return out


def nasa(q, portrait):
    """NASA Image and Video Library (public API, no key). NASA media is generally not copyrighted; only used
    for space/science queries so it never replaces on-location footage."""
    if not q or not re.search(r"space|rocket|planet|earth|moon|mars|star|galaxy|satellite|astronaut|nasa|orbit|sun|launch|storm|hurricane", q, re.I):
        return []
    cf = CACHE / ("n_" + hashlib.md5(q.encode()).hexdigest() + ".json")
    try:
        if cf.exists():
            out = json.loads(cf.read_text())
        else:
            items = json.loads(_get("https://images-api.nasa.gov/search?" + urllib.parse.urlencode(
                {"q": q, "media_type": "video", "page_size": 6}), 15)).get("collection", {}).get("items", [])
            out = []
            for it in items[:6]:
                nid = (it.get("data") or [{}])[0].get("nasa_id", "")
                try:
                    files = json.loads(_get(urllib.parse.quote(it["href"], safe=":/~"), 15))
                except Exception:
                    continue
                files = [urllib.parse.quote(f, safe=":/~") for f in files]
                mp4 = next((f for f in files if f.endswith("~mobile.mp4")), None) or next((f for f in files if f.endswith("~small.mp4")), None) \
                    or next((f for f in files if f.endswith("~medium.mp4")), None)
                thumb = next((l.get("href") for l in it.get("links", []) if l.get("href")), "")
                if mp4 and thumb:
                    out.append({"id": "na" + nid[:40], "url": mp4.replace("http://", "https://"), "thumb": thumb, "dur": 20.0,
                                "w": 1280, "h": 720, "fit": not portrait, "q": q, "tags": nid, "credit": "NASA"})
            cf.write_text(json.dumps(out))
    except Exception:
        return []
    return out


def variants(queries):
    """More ways to say each query: the original, a shorter 2-word core, and a generic fallback without the place name."""
    out, seen = [], set()
    places = r"\b(lagos|nigeria|nigerian|african?|abuja|ibadan|accra|kenya|nairobi)\b"
    for q in queries:
        for v in (q, " ".join(q.split()[-2:]), re.sub(places, "", q, flags=re.I).strip()):
            v = re.sub(r"\s+", " ", v).strip()
            if v and v.lower() not in seen:
                seen.add(v.lower()); out.append(v)
    return out[:9]


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
        for q in variants(s["queries"]):
            src = pixabay(q, portrait) + pexels(q, portrait)
            if q in s["queries"][:2]:  # free archives only for the main queries (keeps request count low)
                src += commons(q, portrait) + nasa(q, portrait)
            for c in src:
                if c["id"] not in seen:
                    seen.add(c["id"]); pool.append(c)
        # pre-rank cheaply: matching orientation, HD, long enough, earlier (more literal) query
        qi = {q: i for i, q in enumerate(s["queries"])}
        pool.sort(key=lambda c: (not c["fit"], min(c["w"], c["h"]) < 720, qi.get(c["q"], 5)))
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
                    raw = scores.get((bi, j), (0,))[0]
        if best:
            best["raw"] = raw if raw < 50 else 0  # 0 = never scored
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


KB = [  # Ken Burns moves (zoom expr, x expr, y expr); N = total frames, on = frame index
    ("1.0+0.14*on/N", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                       # slow push in
    ("1.14-0.14*on/N", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                      # pull out
    ("1.12", "(iw-iw/zoom)*on/N", "ih/2-(ih/zoom/2)"),                                # pan left to right
    ("1.12", "(iw-iw/zoom)*(1-on/N)", "ih/2-(ih/zoom/2)"),                            # pan right to left
    ("1.04+0.10*on/N", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*(1-on/N)"),                  # rise up while pushing in
    ("1.12-0.06*on/N", "(iw-iw/zoom)*0.3*(1+on/N)", "(ih-ih/zoom)*0.6*(1-on/N*0.5)"),  # diagonal drift
]


def cut(src, dst, dur, W, H, look, still=False, zoom=True, kb=0, anim=False):
    """Crop-to-fill, light grade (same for every clip), subtle push-in on static/slow shots, exact duration."""
    grade = "null" if anim else f"eq=contrast=1.06:saturation=1.05,{GRADE.get(look, 'null')}"
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    frames = max(1, int(dur * 30))
    if still:
        z, x, y = (e.replace("N", str(frames)) for e in KB[kb % len(KB)])
        vf = (f"scale={int(W*1.5)}:{int(H*1.5)}:force_original_aspect_ratio=increase,crop={int(W*1.5)}:{int(H*1.5)},"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps=30,{grade},format=yuv420p")
        if anim:  # stick-figure doodle feel: gentle hand-held wobble + pop-in from paper white
            vf += (f",rotate='0.008*sin(2*PI*t*1.3)':fillcolor=0xF5F2EA:ow={W}:oh={H}"
                   f",fade=t=in:st=0:d=0.18:color=0xF5F2EA,format=yuv420p")
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


def fade_plan(lines, durs, xf):
    """Crossfade only where it fits: calm, longer shots. Keep hard cuts after the hook, on short punchy lines,
    and on lines ending in ! or ? (energy beats)."""
    out = []
    for i, l in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        ok = (i >= 1 and nxt and durs[i] >= 2.6 and durs[i + 1] >= 2.6 and not l.rstrip().endswith(("!", "?"))
              and len(l.split()) >= 7 and not re.match(r"^(but|now|so|and then|suddenly|here)", nxt.lower()))
        out.append(bool(ok))
    return out


def join(clips, fades, xf, dst):
    """One file: xfade where fades[i] (clip i was cut xf longer to keep sync), hard concat elsewhere.
    Every input is scaled/padded to the first clip's size (xfade needs identical size, fps, timebase, SAR and format),
    fades are skipped next to clips shorter than 2*xf (negative offsets broke xfade), and if the graph still fails
    the clips are hard-joined with the concat demuxer so a render never dies at the join step."""
    info = [probe(c) for c in clips]
    W, H = info[0][0] or 720, info[0][1] or 1280
    ins, graph, acc, t = [], [], "[n0]", info[0][2]
    for k, c in enumerate(clips):
        ins += ["-i", str(c)]
        graph.append(f"[{k}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                     f"fps=30,settb=AVTB,setsar=1,format=yuv420p[n{k}]")
    nf = 0
    for i in range(1, len(clips)):
        d = info[i][2]
        lab = f"[j{i}]"
        if fades[i - 1] and d > 2 * xf and info[i - 1][2] > 2 * xf and t - xf > 0:
            graph.append(f"{acc}[n{i}]xfade=transition=fade:duration={xf}:offset={t - xf:.3f}{lab}")
            t = t - xf + d; nf += 1
        else:
            graph.append(f"{acc}[n{i}]concat=n=2:v=1:a=0{lab}")
            t += d
        acc = lab
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-filter_complex_threads", "1", *ins, "-filter_complex", ";".join(graph), "-map", acc,
                        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "1", "-pix_fmt", "yuv420p", str(dst)],
                       timeout=600, check=True, capture_output=True)
        return nf
    except subprocess.CalledProcessError as e:
        print("join: xfade graph failed, hard-joining instead:", e.stderr.decode("utf-8", "replace")[-300:])
    norm = []
    for k, c in enumerate(clips):  # re-encode to one format, then lossless concat (low RAM: one clip at a time)
        o = str(dst) + f".n{k}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(c), "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1,format=yuv420p", "-an", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-threads", "1", o], timeout=300, check=True, capture_output=True)
        norm.append(o)
    lst = str(dst) + ".txt"
    Path(lst).write_text("".join(f"file '{n}'\n" for n in norm))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", str(dst)],
                   timeout=300, check=True, capture_output=True)
    for n in norm + [lst]:
        Path(n).unlink(missing_ok=True)
    return 0


def direct(jid, topic, script, secs, voice, rate, aspect, workdir, log=print, progress=None, crossfade=True, style="",
           visuals="stock", ai_style="realistic", ai_prompts=None, keep_script=False, flow=None, chars=None, free_ai=True):
    """Full director pass. Returns dict(script, clips=[paths], clip_max, report)."""
    W, H = (1280, 720) if aspect == "16:9" else (720, 1280)  # same as the final render: less RAM, no double scaling
    workdir = Path(workdir); workdir.mkdir(parents=True, exist_ok=True)
    P = progress or (lambda n: None)
    log("director: writing + critiquing script"); P(3)
    if script and keep_script:
        sc = {"script": script, "draft": script, "critique": "kept as written", "hook_before": "", "hook_after": ""}
    else:
        sc = write_script(topic or script[:120], secs, given=script if script and not topic else "", style=style)
    text = sc["script"]
    lines = sentences(text)
    text = " ".join(lines)
    log(f"director: planning {len(lines)} shots"); P(9)
    look, shots = plan_shots(lines, topic or text[:120])
    pools = candidates(shots, aspect != "16:9"); P(13)
    picks, nimg = score_and_pick(lines, shots, pools, look)
    log(f"director: scored {nimg} candidates in one vision call"); P(18)
    ai = [None] * len(lines)
    aimeta, provs = {}, set()
    if visuals == "ai":
        import aivisuals as aiv
        import charlib
        cast = [c for c in (chars or []) if c.get("look")]
        ctopic = (topic or text[:120]) + ("" if not cast else " | Saved characters (use them exactly): " + charlib.lock_clause(cast))
        aip, aimeta = aiv.prompts(lines, ctopic, ai_style, gem, aspect=aspect)
        det = charlib.detect(lines, cast)
        for i, cs in enumerate(det):  # lock each detected character's description into its scene prompt
            if cs and i < len(aip):
                aip[i] = charlib.lock_clause(cs) + " " + aip[i]
        aimeta["chars"] = [[c["name"] for c in cs] for cs in det]
        aimeta["char_refs"] = [[r for c in cs for r in c.get("refs", [])] for cs in det]
        if cast:
            log("director: characters per scene " + "; ".join(f"{i + 1}:{'+'.join(n) or '-'}" for i, n in enumerate(aimeta["chars"])))
        for i, ov in enumerate((ai_prompts or [])[:len(lines)]):
            if ov:
                aip[i] = ov if "Story bible" in ov else f"{ov}. {aiv.STYLES.get(ai_style, '')}"
        seed = aiv.seed_for(topic or text[:120])  # same seed + same style tokens on every shot = visual continuity
        fl = {}
        if flow:  # Flow (Nano Banana Pro / Veo) first, when the user's bridge is online
            log("director: sending scenes to Google Flow via the Mac bridge"); P(18)
            try:
                fl = flow(lines, aip, aimeta) or {}
            except Exception as e:
                log(f"director: Flow bridge failed {type(e).__name__}")
            if len(fl) < len(lines):
                log(f"director: Using backup {'images' if free_ai else 'stock footage'} for {len(lines) - len(fl)} scene(s) (Flow not available)")
        for i in range(len(lines)):
            if i in fl:
                ai[i] = {"path": fl[i]["path"], "prompt": aip[i], "provider": fl[i]["model"], "video": fl[i].get("video"),
                         "score": fl[i].get("score")}; provs.add("Google Flow")
                continue
            if picks[i] and picks[i].get("raw", 0) >= 8 and not (ai_prompts and i < len(ai_prompts) and ai_prompts[i]):
                continue  # hybrid: a stock clip only when it is a near-perfect match
            if not free_ai:  # free image models are admin-only; other users get stock for unfilled scenes
                continue
            try:
                path, prov, pr, scv, tries = aiv.generate_verified(aip[i], lines[i], W, H, seed, gem, log=log)
                ai[i] = {"path": path, "prompt": pr, "provider": prov, "score": scv, "tries": len(tries), "backup": bool(flow)}
                provs.add(prov)
            except Exception:
                pass
            P(18 + int(2 * (i + 1) / len(lines)))
        log(f"director: AI images for {sum(1 for a in ai if a)}/{len(lines)} shots via {', '.join(sorted(provs)) or 'none'}")
    words, total = word_times(text, voice, rate, str(workdir / f"{jid[:8]}_probe.mp3"))
    durs = sentence_durations(lines, words, total)
    XF = 0.4
    fades = fade_plan(lines, durs, XF) if crossfade else [False] * len(lines)
    log("director: cutting clips")
    clips, rep, cfade = [], [], []
    for i, (l, s, p, d) in enumerate(zip(lines, shots, picks, durs)):
        if ai[i]:
            out = workdir / f"{jid[:8]}_{i:02d}.mp4"
            try:
                cut(ai[i]["path"], str(out), d + (XF if fades[i] else 0), W, H, look, still=not ai[i].get("video"), kb=i, anim=ai_style == "stick")
                clips.append(str(out)); cfade.append(fades[i])
                rep.append({"line": l, "visual": "AI", "prompt": ai[i]["prompt"], "provider": ai[i]["provider"], "score": ai[i].get("score"),
                            "tries": ai[i].get("tries", 1), "backup": ai[i].get("backup", False), "dur": round(d, 2),
                            "characters": (aimeta.get("chars") or [[]] * 99)[i] if i < len(aimeta.get("chars") or []) else []})
                continue
            except Exception as e:
                log(f"director: AI clip {i} failed {type(e).__name__}")
        if not p:
            continue
        raw = workdir / f"{jid[:8]}_raw{i:02d}.mp4"
        try:
            raw.write_bytes(_get(p["url"], 90))
            out = workdir / f"{jid[:8]}_{i:02d}.mp4"
            cut(str(raw), str(out), d + (XF if fades[i] else 0), W, H, look, zoom=(i % 2 == 0))
            clips.append(str(out)); cfade.append(fades[i])
            if progress:
                progress(20 + int(9 * (i + 1) / len(lines)))
        except Exception as e:
            log(f"director: clip {i} failed {type(e).__name__}")
        finally:
            raw.unlink(missing_ok=True)
        rep.append({"line": l, "visual": s.get("visual"), "queries": s.get("queries"), "pick": p.get("id"), "q": p.get("q"),
                    "score": p.get("score"), "why": p.get("why"), "dur": round(d, 2)})
    clip_max = math.ceil(max(durs)) + 1
    if crossfade and len(clips) >= 2 and any(cfade[:-1]):
        try:
            joined = str(workdir / f"{jid[:8]}_joined.mp4")
            n = join(clips, cfade, XF, joined)
            log(f"director: joined {len(clips)} clips with {n} crossfades")
            for c in clips:
                Path(c).unlink(missing_ok=True)
            clips, clip_max = [joined], math.ceil(probe(joined)[2]) + 2
        except Exception as e:
            err = getattr(e, "stderr", b"") or b""
            err = " | ".join(x for x in err.decode("utf8", "replace").splitlines() if "onfig" not in x)[-300:]
            log(f"director: crossfade join failed {type(e).__name__} {err}, using hard cuts")
    print("director report:", json.dumps({"look": look, "critique": sc["critique"], "hook": [sc["hook_before"], sc["hook_after"]], "shots": rep})[:6000], flush=True)
    return {"script": text, "clips": clips, "clip_max": clip_max, "look": look, "report": rep,
            "ai": aimeta, "providers": sorted(provs), "draft": sc["draft"], "critique": sc["critique"], "hook": [sc["hook_before"], sc["hook_after"]]}
