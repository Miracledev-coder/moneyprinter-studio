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


STOP = {"for", "the", "and", "with", "how", "to", "of", "in", "on", "a", "an", "ads", "brands", "brand", "tips", "best"}


def wikipedia(topic):
    words = [w for w in re.findall(r"[A-Za-z0-9]+", topic) if w.lower() not in STOP and len(w) > 1]
    syn = {"ugc": "user-generated content", "ai": "artificial intelligence", "seo": "search engine optimization"}
    words = [syn.get(w.lower(), w) for w in words]
    queries = [" ".join(words)] + [" ".join(words[i:i + 2]) for i in range(len(words) - 1)] + words
    seen, out = set(), []
    for qq in queries[:7]:
        d = json.loads(_get("https://en.wikipedia.org/w/api.php?action=query&list=search&srlimit=3&format=json&srsearch="
                            + urllib.parse.quote(qq), 5))
        for s in d.get("query", {}).get("search", []):
            title = s["title"]
            if title in seen or not any(re.search(r"\b" + re.escape(w) + r"\b", title, re.I) for w in words):
                continue
            seen.add(title)
            try:
                sm = json.loads(_get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_")), 5))
                ext = (sm.get("extract") or "")[:300]
            except Exception:
                ext = re.sub("<[^>]+>", "", s.get("snippet", ""))
            out.append({"title": title, "link": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")), "meta": ext})
        if len(out) >= 3:
            break
    return out[:3]


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


WPS = 2.5  # ~150 spoken words per minute
SHORT_MAX, LONG_MAX = 180, 1200


def target_seconds(b):
    """Script length from the idea generator: seconds (int/str like '45', '90s', '3m', '8 min'); Shorts max 3 min."""
    fmt = "long" if b.get("format") == "long" else "short"
    v = str(b.get("length") or "").strip().lower()
    secs = {"short": 15, "medium": 40, "long": 75}.get(v)
    if secs is None:
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)?$", v)
        secs = (float(m.group(1)) * (60 if (m.group(2) or "").startswith("m") else 1)) if m else (480 if fmt == "long" else 40)
    lo, hi = (60, LONG_MAX) if fmt == "long" else (10, SHORT_MAX)
    return fmt, int(max(lo, min(secs, hi)))


def _wc(t):
    return len(re.findall(r"[A-Za-z0-9'’-]+", t or ""))


def script(b):
    fmt, secs = target_seconds(b)
    words = int(round(secs * WPS))
    ask = int(words * (1.0 if secs < 45 else 1.2 if secs <= 180 else 1.3))  # drafts run ~20-30% short; aim higher
    paras = max(2, round(ask * 0.8 / 110))
    i = b.get("idea") or {}
    base = f"""Title: {i.get('title')}\nHook: {i.get('hook')} ({i.get('hook_type')})\nWhy: {i.get('why')}
Goal: {b.get('goal')}. Platform: {b.get('platform')}. Audience: {b.get('audience') or 'general'}. Tone: {b.get('tone') or 'confident, friendly'}.
Plain spoken English, no emojis, no stage directions, no headings, no hashtags inside the script."""
    if fmt == "long":
        p = f"""Write a long-form YouTube voice-over script for this idea.
{base}
WORD BUDGET: the spoken script (hook + body + cta) must be {ask} words (±10%); write the body as {paras} paragraphs of ~110 words each, i.e. {secs // 60} min {secs % 60} s at 150 words per minute.
Structure: hook (2-3 sentences), then a body of several sections as paragraphs separated by blank lines (each section opens with a smooth spoken transition), then a short CTA.
Return JSON: {{"hook":str,"tension":str (the body, paragraphs separated by \\n\\n),"payoff":"","cta":str,"caption":str (1-2 lines),"hashtags":[3-5 strings starting with #],"keywords":[3-5 stock footage search terms]}}"""
    else:
        p = f"""Write a short-form video voice-over script for this idea.
{base}
WORD BUDGET: target {secs} seconds = {ask} spoken words TOTAL across hook+tension+payoff+cta (±10%; count them). Section budgets: hook ~{max(6, ask * 8 // 100)} words (start with the hook line), tension ~{ask * 50 // 100} words ({max(1, paras - 1)} short paragraphs) (several concrete beats/examples), payoff ~{ask * 30 // 100} words, cta ~{max(6, ask * 12 // 100)} words. Writers usually come in short: hit the budget.
Return JSON: {{"hook":str,"tension":str,"payoff":str,"cta":str,"caption":str (1-2 lines for the post),"hashtags":[3-5 strings starting with #],"keywords":[3-5 stock footage search terms]}}"""
    to = 150 if fmt == "long" else 60
    d = _gemini(p, timeout=to)
    if isinstance(d, list): d = d[0] if d else {}
    full = lambda x: " ".join(str(x.get(k, "")).strip() for k in ("hook", "tension", "payoff", "cta") if str(x.get(k, "")).strip())
    first = _wc(full(d)); d["retried"] = False
    if abs(first - words) > 0.15 * words:  # off by more than 15%: one more pass to expand or trim
        how = "EXPAND" if first < words else "TRIM"
        p2 = p + f"""

Your previous draft had {first} words but the budget is {words}. {how} it to {words} words (±10%), keeping the same hook, facts and structure.{" Add new concrete details, examples and beats (aim for " + str(int(min(1.7 * words, words * words / max(first, 1) * 1.05))) + " words; drafts usually come in short)." if how == "EXPAND" else ""}
Previous draft JSON: {json.dumps({k: d.get(k, "") for k in ("hook", "tension", "payoff", "cta")})}"""
        try:
            d2 = _gemini(p2, timeout=to)
            if isinstance(d2, list): d2 = d2[0] if d2 else {}
            if abs(_wc(full(d2)) - words) < abs(first - words):
                for k in ("caption", "hashtags", "keywords"):
                    d2.setdefault(k, d.get(k))
                d = d2
            d["retried"] = True
        except Exception:
            d["retried"] = True
    d["full"] = full(d) if fmt == "short" else "\n\n".join(str(d.get(k, "")).strip() for k in ("hook", "tension", "cta") if str(d.get(k, "")).strip())
    d["hashtags"] = [h if str(h).startswith("#") else "#" + str(h) for h in (d.get("hashtags") or [])][:5]
    d.update(seconds=secs, format=fmt, words=_wc(d["full"]), target_words=words, first_words=first)
    return d
