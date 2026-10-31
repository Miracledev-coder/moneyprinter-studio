"""Director 'Style presets' built from Miracle's reference channels (presets/director_style_presets.json, STYLE_GUIDE.md).

A preset drives: the image prompt template + negative prompt, a character bible, shot pacing (~3 s per shot, so long
sentences get several images), camera motion rules (hard cuts, Ken Burns), the hook/script structure and title formulas,
and subtitle/music suggestions shown in the UI."""
import json, re
from pathlib import Path

_F = Path(__file__).with_name("presets") / "director_style_presets.json"
DEFAULT_LONG = "ancient_ink"


def load():
    try:
        d = json.loads(_F.read_text())
        ps = d.get("presets", d)
        return {p["id"]: p for p in ps} if isinstance(ps, list) else ps
    except Exception:
        return {}


P = load()


def get(pid):
    return P.get(pid or "")


def public():
    """What the picker shows."""
    return [{"id": p["id"], "name": p["name"], "visual_style": p.get("visual_style", ""), "subtitle_style": p.get("subtitle_style", ""),
             "music_mood": p.get("music_mood", ""), "title_formulas": p.get("title_formulas", [])[:5],
             "avg_shot_seconds": p.get("avg_shot_seconds", 3), "default_long": p["id"] == DEFAULT_LONG} for p in P.values()]


def script_style(p):
    """Extra FORMAT instructions for write_script."""
    return (f"Write in the '{p['name']}' YouTube style. Structure: " + " | ".join(p.get("script_structure_template", []))
            + ". Hook formulas to adapt (fill the braces, never print them): " + " / ".join(p.get("hook_formulas", [])[:3])
            + f". Pace about {p.get('words_per_minute', 170)} words per minute, short punchy sentences, a twist every chapter.")


def title_hint(p):
    return "Title formulas: " + " / ".join(p.get("title_formulas", [])[:5])


def max_words(p, hook=False):
    secs = p.get("hook_shot_seconds" if hook else "avg_shot_seconds", 3.0) or 3.0
    return max(5, round(secs * p.get("words_per_minute", 170) / 60))


def split_shots(lines, p):
    """One shot per ~avg_shot_seconds of narration: long sentences are cut at clause boundaries into several shots
    (the reference channels average ~1.2 images per sentence). The narration text itself is unchanged."""
    out = []
    for k, l in enumerate(lines):
        mw = max_words(p, hook=k == 0)
        w = l.split()
        n = max(1, round(len(w) / mw))
        if n == 1:
            out.append(l); continue
        size = len(w) / n
        cuts, prev = [], 0
        for c in range(1, n):  # snap each cut to the nearest comma/conjunction within 3 words
            t = round(c * size)
            best = t
            for d in range(6):
                for cand in (t - d, t + d):
                    if prev + 1 < cand < len(w) - 2 and (w[cand - 1].endswith((",", ";", ":", "—")) or w[cand].lower() in ("and", "but", "so", "because", "which", "while", "then", "until", "when", "until", "who")):
                        best = cand; break
                else:
                    continue
                break
            if best > prev + 1:
                cuts.append(best); prev = best
        b = 0
        for c in cuts + [len(w)]:
            out.append(" ".join(w[b:c])); b = c
    return out


def bible_hint(p):
    cb = p.get("character_bible_template") or {}
    return "Character bible format (fill every field, reuse it in every scene): " + "; ".join(f"{k}: {v}" for k, v in cb.items() if k != "id")


def fill(p, sc, bible, aspect):
    """Image prompt from the preset template; unknown {placeholders} and optional clauses are dropped."""
    t = p.get("image_prompt_template", "")
    vals = {"scene_description": sc.get("subject_action", ""), "character_refs": (bible or "")[:300], "setting": sc.get("setting", ""),
            "time_of_day": sc.get("time_of_day") or "day", "mood": sc.get("lighting_mood", ""), "lighting": sc.get("lighting_mood") or "soft",
            "ottoman_location": sc.get("setting", ""), "year": sc.get("era", ""), "costume": sc.get("costume", "period costume"),
            "environment": sc.get("setting", ""), "labels_or_arrows_or_map": sc.get("label", "none")}
    t = re.sub(r"\{optional_[a-z_]+:[^{}]*(\{[^{}]*\}[^{}]*)*\}", (lambda m: "") if not sc.get("label") else
               (lambda m: f'small handwritten label reading "{sc["label"][:30]}"'), t)
    t = re.sub(r"\{background:[^{}]*(\{[^{}]*\}[^{}]*)*\}", "plain white background" if sc.get("card") else f"painted {sc.get('setting', 'landscape')}", t)
    t = re.sub(r"\{([a-z_]+)\}", lambda m: str(vals.get(m.group(1), "")), t)
    t = re.sub(r"\s*\(\s*\)", "", t.replace("16:9", f"aspect ratio {aspect}"))
    return re.sub(r"\s+", " ", re.sub(r"(\.\s*)+\.", ".", t)).strip()


def motion(p):
    return "; ".join(p.get("camera_motion_rules", []))


def hard_cuts(p):
    return any("hard cut" in r.lower() for r in p.get("camera_motion_rules", []))
