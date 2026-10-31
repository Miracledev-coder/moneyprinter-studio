"""Long Script Mode: split long narration into ~2-3 min parts, metadata for YouTube."""
import json, re, subprocess
from ideas import _gemini
from scenes import sentences

WPM = 150  # Edge neural voices at normal speed


def split_parts(script, max_words=380):
    paras = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", script or "") if p.strip()]
    units = []
    for p in paras:  # paragraphs longer than a part are cut at sentence breaks
        if len(p.split()) <= max_words:
            units.append(p)
        else:
            units += sentences(p)
    parts, cur = [], []
    for u in units:
        if cur and len(" ".join(cur + [u]).split()) > max_words:
            parts.append("\n\n".join(cur)); cur = [u]
        else:
            cur.append(u)
    if cur:
        parts.append("\n\n".join(cur))
    # fold a tiny last part into the previous one
    if len(parts) > 1 and len(parts[-1].split()) < max_words * 0.25:
        parts[-2] += "\n\n" + parts.pop()
    return parts


def meta(title, parts, hint=""):
    prompt = ("You help publish long-form YouTube videos. Given the numbered parts of a narration, return JSON: "
              '{"sections": ["short chapter title for part 1", ...], "title": "catchy YouTube title under 70 chars", '
              '"description": "2-3 sentence YouTube description plus 3-5 hashtags"} with exactly ' + str(len(parts)) +
              f" sections (each under 6 words).\n{hint}\nWorking title: {title}\n\n" +
              "\n".join(f"{i+1}. {p[:500]}" for i, p in enumerate(parts)))
    try:
        d = _gemini(prompt, timeout=45)
        secs = [str(s)[:60] for s in (d.get("sections") or [])]
        out = {"sections": secs, "title": str(d.get("title") or title)[:100], "description": str(d.get("description") or "")[:1500]}
    except Exception:
        out = {"sections": [], "title": title, "description": ""}
    while len(out["sections"]) < len(parts):
        out["sections"].append(f"Part {len(out['sections']) + 1}")
    out["sections"] = out["sections"][:len(parts)]
    return out


def duration(path):
    from scenes import probe
    return probe(path)[2]


def concat(paths, out):
    lst = str(out) + ".txt"
    with open(lst, "w") as f:
        for p in paths:
            f.write("file '" + str(p).replace("'", "'\\''") + "'\n")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                        "-movflags", "+faststart", str(out)], capture_output=True, timeout=900)
    import os; os.unlink(lst)
    if r.returncode != 0:
        raise RuntimeError("joining parts failed: " + r.stderr.decode()[-200:])


def chapters(sections, durs):
    t, lines = 0.0, []
    for s, d in zip(sections, durs):
        m, sec = divmod(int(t), 60); h, m = divmod(m, 60)
        lines.append((f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}") + " " + s)
        t += d
    return "\n".join(lines)
