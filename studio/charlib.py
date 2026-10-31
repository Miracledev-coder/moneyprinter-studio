"""Per-user saved characters (locked look + 1-3 reference images) and music tags, mirrored to the user's Drive."""
import json, re, threading, uuid
from pathlib import Path

MAX_CHARS, MAX_REFS = 20, 3
PRONOUN = re.compile(r"\b(he|she|they|him|her|his|hers|i|me|my|we|our)\b", re.I)


class Lib:
    def __init__(self, root, ufx, gd):
        self.root, self.ufx, self.gd = Path(root), ufx, gd
        (self.root / "chars").mkdir(parents=True, exist_ok=True)

    # ---------- storage (local first, Drive copy so it survives free-plan restarts)
    def _f(self, u, kind):
        return self.root / f"{kind}_{self.ufx(u)}.json"

    def load(self, u, kind="chars"):
        f = self._f(u, kind)
        if f.exists():
            try:
                return json.loads(f.read_text())
            except Exception:
                pass
        try:
            _, acct = self.gd.target_for(u)
            d = acct.get_json(f"studio_{kind}.json") if acct else None
            if isinstance(d, list):
                f.write_text(json.dumps(d))
                return d
        except Exception:
            pass
        return []

    def save(self, u, data, kind="chars"):
        self._f(u, kind).write_text(json.dumps(data))

        def push():
            try:
                _, acct = self.gd.target_for(u)
                if acct:
                    acct.put_json(f"studio_{kind}.json", data)
            except Exception:
                pass
        threading.Thread(target=push, daemon=True).start()

    # ---------- characters
    def upsert(self, u, b, clean):
        cs = self.load(u)
        cid = str(b.get("id") or "")
        c = next((x for x in cs if x["id"] == cid), None)
        if not c:
            if len(cs) >= MAX_CHARS:
                raise ValueError(f"You can save up to {MAX_CHARS} characters.")
            c = {"id": uuid.uuid4().hex[:10], "refs": []}
            cs.append(c)
        c["name"] = clean(b.get("name"), 40) or c.get("name") or "Character"
        c["look"] = clean(b.get("look"), 600) or c.get("look", "")
        c["style"] = b.get("style") if b.get("style") in ("realistic", "pixar", "stick") else c.get("style", "realistic")
        c["main"] = bool(b.get("main", c.get("main", False)))
        self.save(u, cs)
        return c

    def delete(self, u, cid):
        cs = self.load(u)
        for c in cs:
            if c["id"] == cid:
                for r in c.get("refs", []):
                    (self.root / "chars" / r).unlink(missing_ok=True)
        self.save(u, [c for c in cs if c["id"] != cid])

    def add_ref(self, u, cid, data: bytes):
        from PIL import Image
        import io
        cs = self.load(u)
        c = next((x for x in cs if x["id"] == cid), None)
        if not c:
            raise KeyError("character")
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1024, 1024))
        name = f"{self.ufx(u)}{cid}_{uuid.uuid4().hex[:6]}.jpg"
        im.save(self.root / "chars" / name, "JPEG", quality=88)
        c["refs"] = (c.get("refs", []) + [name])[-MAX_REFS:]
        self.save(u, cs)
        self._push_ref(u, name)
        return c

    def _push_ref(self, u, name):
        def push():
            try:
                _, acct = self.gd.target_for(u)
                if acct:
                    acct.upload(str(self.root / "chars" / name), "studio_char_" + name, "image/jpeg", acct.find("studio_char_" + name))
            except Exception:
                pass
        threading.Thread(target=push, daemon=True).start()

    def ref_path(self, u, name):
        name = Path(name).name
        if not name.startswith(self.ufx(u)):
            return None
        p = self.root / "chars" / name
        if not p.exists():
            try:
                _, acct = self.gd.target_for(u)
                fid = acct.find("studio_char_" + name) if acct else None
                if fid:
                    acct.download(fid, str(p))
            except Exception:
                pass
        return p if p.exists() else None

    @staticmethod
    def portrait_prompt(c):
        from aivisuals import STYLES
        return (f"Character reference sheet portrait of {c['name']}: {c['look']}. Full body and face clearly visible, "
                f"neutral standing pose, plain light grey background, even studio lighting. {STYLES.get(c.get('style'), '')}")


def detect(lines, chars):
    """Which saved characters appear in each sentence: by name, else the 'main' character when the line is about a person."""
    out = []
    for ln in lines:
        hit = [c for c in chars if c.get("name") and re.search(r"\b" + re.escape(c["name"]) + r"\b", ln, re.I)]
        if not hit:
            hit = [c for c in chars if c.get("main") and PRONOUN.search(ln)]
        out.append(hit)
    return out


def lock_clause(cs):
    return " ".join(f"Character {c['name']} (keep exactly the same look in every scene): {c['look']}." for c in cs if c.get("look"))
