"""Idea & research generator: free signals (Google Trends RSS, Google News RSS, Reddit JSON,
Wikipedia) + Gemini flash-lite for angles and scripts. Small, cached, time-boxed."""
import json, os, re, threading, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait

UA = {"User-Agent": "MoneyPrinterStudio/1.0 (personal research tool)"}
_cache, _lock = {}, threading.Lock()
TTL = 1800


def _get(url, timeout=6, limit=600_000):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def _cached(key, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    val = fn()
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = (now, val)
    return val


def _rss(url, n):
    root = ET.fromstring(_get(url))
    out = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        if t:
            extra = it.findtext("{https://trends.google.com/trending/rss}approx_traffic") or it.findtext("source") or ""
            out.append({"title": t, "link": (it.findtext("link") or "").strip(), "meta": extra.strip()})
        if len(out) >= n:
            break
    return out


def trends(_topic):
    items = []
    for geo in ("NG", "US"):
        for x in _rss(f"https://trends.google.com/trending/rss?geo={geo}", 6):
            x["meta"] = f"{geo} · {x['meta']} searches".strip(" ·")
            items.append(x)
    return items


def news(topic):
    q = urllib.parse.quote(topic)
    return _rss(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en", 8)


def reddit(topic):
    q = urllib.parse.quote(topic)
    try:
        return _reddit_json(q)
    except Exception:
        pass
    A = "{http://www.w3.org/2005/Atom}"
    root = ET.fromstring(_get(f"https://www.reddit.com/search.rss?q={q}&sort=top&t=month&limit=8"))
    out = []
    for e in root.iter(A + "entry"):
        l = e.find(A + "link"); c = e.find(A + "category")
        out.append({"title": e.findtext(A + "title") or "", "link": l.get("href") if l is not None else "",
                    "meta": ("r/" + c.get("term")) if c is not None else "top this month"})
        if len(out) >= 8: break
    return out


def _reddit_json(q):
    d = json.loads(_get(f"https://www.reddit.com/search.json?q={q}&sort=top&t=month&limit=8&raw_json=1"))
    out = []
    for c in d.get("data", {}).get("children", []):
        p = c.get("data", {})
        out.append({"title": p.get("title", ""), "link": "https://www.reddit.com" + p.get("permalink", ""),
                    "meta": f"r/{p.get('subreddit')} · {p.get('score', 0)} upvotes · {p.get('num_comments', 0)} comments"})
    return out


def wikipedia(topic):
    q = urllib.parse.quote(topic)
    d = json.loads(_get(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&srlimit=3&format=json"))
    out = []
    for s in d.get("query", {}).get("search", []):
        title = s["title"]
        try:
            sm = json.loads(_get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_")), 5))
            ext = (sm.get("extract") or "")[:300]
        except Exception:
            ext = re.sub("<[^>]+>", "", s.get("snippet", ""))
        out.append({"title": title, "link": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")), "meta": ext})
    return out


SOURCES = {"Google Trends (daily, NG + US)": trends, "Google News": news, "Reddit": reddit, "Wikipedia": wikipedia}


def research(topic):
    topic = topic.strip()[:120]
    def run(name, fn):
        try:
            return {"name": name, "ok": True, "items": _cached(f"{name}|{topic.lower()}", lambda: fn(topic))}
        except Exception as e:
            return {"name": name, "ok": False, "items": [], "error": type(e).__name__ + ": " + str(e)[:80]}
    with ThreadPoolExecutor(4) as ex:
        futs = [ex.submit(run, n, f) for n, f in SOURCES.items()]
        wait(futs, timeout=14)
    res = []
    for f, n in zip(futs, SOURCES):
        res.append(f.result() if f.done() else {"name": n, "ok": False, "items": [], "error": "timeout"})
    return res


def _gemini(prompt, timeout=45):
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.environ.get("IDEAS_MODEL") or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini error {e.code}") from None
    txt = d["candidates"][0]["content"]["parts"][0]["text"]
    txt = re.sub(r"^```(json)?|```$", "", txt.strip()).strip()
    return json.loads(txt)


def _signals(res, n=6):
    lines = []
    for s in res:
        for it in s["items"][:n]:
            lines.append(f"[{s['name']}] {it['title']} ({it.get('meta', '')[:120]})")
    return "\n".join(lines) or "(no live signals available)"


HOOKS = "curiosity gap, bold claim, mini story, contrarian, list, POV, myth-bust"


def ideas(b, res):
    n = max(5, min(int(b.get("count") or 6), 10))
    p = f"""You are a top short-form video strategist. Create {n} premium, specific video angles.
Niche/topic: {b.get('topic')}
Goal: {b.get('goal')}. Platform: {b.get('platform')}. Audience: {b.get('audience') or 'general'}. Tone: {b.get('tone') or 'confident, friendly'}.
Live research signals (cite them; only use those relevant to the topic, never invent stats):
{_signals(res)}
Return JSON: {{"ideas":[{{"title":str,"hook":str (spoken in first 2 seconds, max 12 words),"hook_type":one of [{HOOKS}],
"why":str (one sentence citing a specific research signal and source name),"score":int 1-10,"keywords":[3-5 short English stock-footage search terms]}}]}}
Vary hook types. Sort by score descending."""
    g = _gemini(p)
    out = (g if isinstance(g, list) else g.get("ideas", []))[:n]
    for i in out:
        i["score"] = max(1, min(int(i.get("score") or 5), 10))
        i["keywords"] = [str(k)[:40] for k in (i.get("keywords") or [])][:5]
    return out


def script(b):
    secs = {"short": 15, "medium": 40, "long": 75}.get(b.get("length"), 15)
    words = int(secs * 2.5)
    i = b.get("idea") or {}
    p = f"""Write a short-form video voice-over script for this idea.
Title: {i.get('title')}\nHook: {i.get('hook')} ({i.get('hook_type')})\nWhy: {i.get('why')}
Goal: {b.get('goal')}. Platform: {b.get('platform')}. Audience: {b.get('audience') or 'general'}. Tone: {b.get('tone') or 'confident, friendly'}.
Target duration {secs} seconds = about {words} spoken words TOTAL. Structure: hook (<=2 seconds, start with the hook line), tension/value, payoff, CTA.
Plain spoken English, no emojis, no stage directions, no hashtags inside the script.
Return JSON: {{"hook":str,"tension":str,"payoff":str,"cta":str,"caption":str (1-2 lines for the post),"hashtags":[3-5 strings starting with #],"keywords":[3-5 stock footage search terms]}}"""
    d = _gemini(p)
    if isinstance(d, list): d = d[0] if d else {}
    d["full"] = " ".join(str(d.get(k, "")).strip() for k in ("hook", "tension", "payoff", "cta")).strip()
    d["hashtags"] = [h if str(h).startswith("#") else "#" + str(h) for h in (d.get("hashtags") or [])][:5]
    d["seconds"] = secs
    return d
