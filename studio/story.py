"""Story studio: YouTube story -> fresh retelling, story ideas, script, cast, scene prompts, thumbnails.
Free tools only (youtube-transcript-api / yt-dlp auto-subs / Gemini free tier)."""
import json, os, re, subprocess, tempfile, urllib.request, urllib.error
from pathlib import Path

WPS = 2.5  # spoken words per second (150 wpm)


def gem(prompt, temp=0.8, timeout=120, video=None):
    import director
    if not video:
        return director.gem(prompt, temp=temp, timeout=timeout)
    key = os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("STORY_VIDEO_MODEL") or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    body = json.dumps({"contents": [{"parts": [{"file_data": {"file_uri": video}}, {"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": temp}}).encode()
    r = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=body,
                               headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as f:
            d = json.loads(f.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini error {e.code}") from None
    t = d["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(re.sub(r"^```(json)?|```$", "", t.strip()).strip())


def video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else (url if re.fullmatch(r"[A-Za-z0-9_-]{11}", url or "") else None)


def _api(vid):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    tl = api.list(vid)
    try:
        t = tl.find_transcript(["en", "en-US", "en-GB"])
    except Exception:
        t = next(iter(tl))
        if t.language_code[:2] != "en" and t.is_translatable:
            t = t.translate("en")
    return " ".join(s.text for s in t.fetch() if s.text.strip())


def _ytdlp(vid):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs", "--sub-langs", "en.*,en", "--sub-format", "vtt",
                        "-o", f"{d}/s.%(ext)s", f"https://www.youtube.com/watch?v={vid}"], capture_output=True, timeout=90)
        fs = sorted(Path(d).glob("*.vtt"))
        if not fs:
            raise RuntimeError("no subtitles")
        out, last = [], ""
        for ln in fs[0].read_text(errors="ignore").splitlines():
            if "-->" in ln or not ln.strip() or ln.startswith(("WEBVTT", "Kind:", "Language:")) or re.fullmatch(r"\d+", ln.strip()):
                continue
            ln = re.sub(r"<[^>]+>", "", ln).strip()
            if ln and ln != last:
                out.append(ln); last = ln
        return " ".join(out)


def transcript(url):
    """Returns (text, source). Tries the transcript API, then yt-dlp auto-subs, then Gemini watching the video."""
    vid = video_id(url)
    if not vid:
        raise ValueError("That doesn't look like a YouTube link.")
    errs = []
    for name, fn in (("captions", _api), ("yt-dlp", _ytdlp)):
        try:
            t = fn(vid)
            if len(t.split()) > 40:
                return t[:60000], name
        except Exception as e:
            errs.append(f"{name}: {type(e).__name__}")
    if os.environ.get("GEMINI_API_KEY"):
        d = gem('Watch this video and write down the full story it tells as a detailed plain-text summary of every plot beat, in order '
                '(characters, setting, conflict, twists, ending). Return JSON {"title":str,"story":str}.', temp=0.3, timeout=180,
                video=f"https://www.youtube.com/watch?v={vid}")
        return (d.get("title", "") + "\n" + d.get("story", "")).strip()[:60000], "gemini-video"
    raise RuntimeError("Couldn't read that video (" + "; ".join(errs) + ")")


def ideas(topic="", n=6):
    d = gem(f"""You pitch viral YouTube story videos (drama, mystery, revenge, redemption, true-story style retellings that keep viewers to the end).
{('Niche / inspiration: ' + topic) if topic else 'Any niche that performs well right now.'}
Pitch {n} ORIGINAL story ideas. Return JSON {{"ideas":[{{"title":str (clickable, <70 chars),"logline":str (2 sentences),"hook":str (first spoken line),"genre":str}}]}}""")
    return [i for i in _items(d, "ideas") if isinstance(i, dict) and i.get("title")][:n]


def script(seconds, source="", idea=None, notes="", style=""):
    words = int(seconds * WPS)
    base = (f"SOURCE STORY (retell it in completely fresh words, new sentences and your own narration voice; keep the plot beats, "
            f"characters' roles and the ending; do not copy any sentence):\n{source[:20000]}") if source else \
           f"STORY IDEA: {json.dumps(idea or {})}"
    d = gem(f"""You are a top YouTube storyteller narrator. Write the full narration for a story video.
{base}
{('Extra notes: ' + notes) if notes else ''}{(' Visual style: ' + style) if style else ''}
LENGTH: about {words} spoken words (±10%) = {seconds // 60} min {seconds % 60} s. Open with a gripping hook line, build tension, twist, satisfying ending, short call to action.
Plain spoken narration only (no scene directions, no headings). Paragraphs separated by blank lines.
Return JSON {{"title":str,"script":str,"logline":str}}""", temp=0.85, timeout=180)
    s = str(d.get("script") or "").strip()
    if not s:
        raise RuntimeError("empty script")
    return {"title": str(d.get("title") or "")[:100], "script": s, "logline": str(d.get("logline") or "")[:400], "words": len(s.split())}


def _items(d, key):
    if isinstance(d, list):
        return d
    return (d or {}).get(key) or []


def cast(text, style="realistic", n=6):
    d = gem(f"""List the recurring characters in this story (max {n}, most important first). For each give a locked visual description an
illustrator can reuse in every scene: age, gender, ethnicity/skin tone, face, hair, build, signature clothing with colours, one distinctive detail.
Style: {style}. STORY:\n{text[:12000]}
Return JSON {{"characters":[{{"name":str,"role":str,"look":str (<=60 words)}}]}}""", temp=0.4)
    return [c for c in _items(d, "characters") if isinstance(c, dict) and c.get("name")][:n]


def scenes(text, chars, style_txt, seconds, aspect="16:9"):
    n = max(3, min(40, round(seconds / (8 if seconds <= 180 else 15))))
    cl = "\n".join(f"- {c['name']}: {c.get('look', '')}" for c in chars)
    d = gem(f"""Split this narration into exactly {n} consecutive scenes (cover the whole text, in order). For each scene write ONE detailed
{aspect} image prompt for Google Flow (Nano Banana Pro): subject + action + setting + camera/framing + lighting + mood, in this visual style: {style_txt}.
When a character appears, name them and repeat their locked look from the cast list word for word. No text or captions in the image.
CAST:\n{cl or '(none)'}\nNARRATION:\n{text[:16000]}
Return JSON {{"scenes":[{{"text":str (the narration lines for this scene),"prompt":str,"characters":[names]}}]}}""", temp=0.5, timeout=180)
    out = []
    for s in _items(d, "scenes"):
        if isinstance(s, dict) and s.get("prompt"):
            out.append({"text": str(s.get("text") or "")[:600], "prompt": str(s["prompt"])[:1800],
                        "characters": [str(x)[:40] for x in (s.get("characters") or []) if x][:4]})
    return out


FONT = Path(__file__).resolve().parent.parent / "resource" / "fonts" / "Anton-Regular.ttf"
SCHEMES = [("#FFE600", "#000000", "left"), ("#FFFFFF", "#E8364A", "right"), ("#FFFFFF", "#000000", "bottom")]


def thumbnail(base_img, title, variant=0, out=None):
    """1280x720 YouTube thumbnail: cover-cropped image, contrast boost, vignette, huge outlined title text."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    W, H = 1280, 720
    im = Image.open(base_img).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((int(im.width * s) + 1, int(im.height * s) + 1))
    fg, stroke, pos = SCHEMES[variant % len(SCHEMES)]
    x0 = {"left": im.width - W, "right": 0}.get(pos, (im.width - W) // 2)  # push the subject away from the text
    im = im.crop((x0, (im.height - H) // 2, x0 + W, (im.height - H) // 2 + H))
    im = ImageEnhance.Contrast(ImageEnhance.Color(im).enhance(1.35)).enhance(1.25)
    shade = Image.new("L", (W, H), 0); d = ImageDraw.Draw(shade)
    if pos == "bottom":
        for y in range(H // 2, H):
            d.line([(0, y), (W, y)], fill=int(200 * (y - H / 2) / (H / 2)))
    else:
        for x in range(W // 2 + 80):
            xx = x if pos == "left" else W - 1 - x
            d.line([(xx, 0), (xx, H)], fill=int(210 * (1 - x / (W / 2 + 80))))
    im = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), im, shade.filter(ImageFilter.GaussianBlur(30)))
    words = (title or "").upper().split()[:9]
    box_w = W - 120 if pos == "bottom" else int(W * 0.52)
    dr = ImageDraw.Draw(im)
    for size in range(150, 40, -6):
        f = ImageFont.truetype(str(FONT), size)
        lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if dr.textlength(t, font=f) <= box_w:
                cur = t
            else:
                lines.append(cur); cur = w
        lines.append(cur); lines = [l for l in lines if l]
        lh = int(size * 1.08)
        if len(lines) <= (2 if pos == "bottom" else 4) and lh * len(lines) <= H - 100:
            break
    th = lh * len(lines)
    y = H - th - 50 if pos == "bottom" else (H - th) // 2
    for i, l in enumerate(lines):
        tw = dr.textlength(l, font=f)
        x = (W - tw) // 2 if pos == "bottom" else (60 if pos == "left" else W - 60 - tw)
        col = fg if i % 2 == 0 else ("#FFFFFF" if fg != "#FFFFFF" else "#FFE600")
        dr.text((x, y + i * lh), l, font=f, fill=col, stroke_width=max(4, size // 14), stroke_fill=stroke)
    out = out or tempfile.mktemp(suffix=".jpg")
    im.save(out, "JPEG", quality=90)
    return out
