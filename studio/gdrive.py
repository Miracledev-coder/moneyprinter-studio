"""Google Drive storage (scope drive.file), per account.

Each user can connect their own Drive: their videos, thumbnails and history.json go to a "MoneyPrinter Studio"
folder in THEIR Drive. Users who have not connected fall back to the admin Drive (GDRIVE_REFRESH_TOKEN) in a
per-user subfolder. Per-user refresh tokens are Fernet-encrypted (key from TOKEN_ENC_KEY or SESSION_SECRET) and kept
in tokens.json inside the admin Drive folder, so they survive Render's disk wipes for free. Tokens never leave the server.
"""
import base64, hashlib, json, os, queue, threading, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/drive.file"
API = "https://www.googleapis.com/drive/v3"
UP = "https://www.googleapis.com/upload/drive/v3/files"
ADMIN = "_admin"
TOKENS_FILE = "tokens.json"
LOCAL_TOKENS = Path(os.environ.get("TOKENS_CACHE", "/tmp/mps_tokens.json"))


def configured():
    return bool(os.environ.get("GDRIVE_CLIENT_ID") and os.environ.get("GDRIVE_CLIENT_SECRET"))


def folder_name():
    return os.environ.get("GDRIVE_FOLDER_NAME") or "MoneyPrinter Studio"


def redirect_uri():
    return (os.environ.get("RENDER_EXTERNAL_URL") or "https://moneyprinter-studio.onrender.com").rstrip("/") + "/oauth/google/callback"


def auth_url(state, hint=""):
    p = dict(client_id=os.environ["GDRIVE_CLIENT_ID"], redirect_uri=redirect_uri(), response_type="code", scope=SCOPE,
             access_type="offline", prompt="consent select_account", include_granted_scopes="true", state=state)
    if hint:
        p["login_hint"] = hint
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(p)


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read()
        return json.loads(t) if t else {}


def exchange(code):
    """Returns (refresh_token, access_token, expires_in)."""
    d = _post_form("https://oauth2.googleapis.com/token", dict(
        code=code, client_id=os.environ["GDRIVE_CLIENT_ID"], client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        redirect_uri=redirect_uri(), grant_type="authorization_code"))
    return d.get("refresh_token", ""), d.get("access_token", ""), int(d.get("expires_in", 0))


def revoke(tok):
    try:
        _post_form("https://oauth2.googleapis.com/revoke", {"token": tok})
    except Exception:  # noqa: BLE001
        pass


class Account:
    """One Drive: a refresh token plus a folder path (e.g. ["MoneyPrinter Studio"] or [..., "users", "bob"])."""

    def __init__(self, refresh, path):
        self.refresh, self.path = refresh, path
        self._tok, self._exp, self._folder = "", 0, ""
        self._lock = threading.Lock()

    def token(self):
        with self._lock:
            if self._tok and time.time() < self._exp:
                return self._tok
            d = _post_form("https://oauth2.googleapis.com/token", dict(
                refresh_token=self.refresh, client_id=os.environ["GDRIVE_CLIENT_ID"],
                client_secret=os.environ["GDRIVE_CLIENT_SECRET"], grant_type="refresh_token"))
            self._tok, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 3600)) - 60
            return self._tok

    def _req(self, method, url, body=None, headers=None, timeout=60):
        h = {"Authorization": "Bearer " + self.token()}
        h.update(headers or {})
        return urllib.request.urlopen(urllib.request.Request(url, data=body, method=method, headers=h), timeout=timeout)

    def _json(self, method, url, obj=None):
        with self._req(method, url, json.dumps(obj).encode() if obj is not None else None,
                       {"Content-Type": "application/json"} if obj is not None else None) as r:
            t = r.read()
            return json.loads(t) if t else {}

    def _child_folder(self, name, parent):
        name_q = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name = '{name_q}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        if parent:
            q += f" and '{parent}' in parents"
        d = self._json("GET", API + "/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}))
        if d.get("files"):
            return d["files"][0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            meta["parents"] = [parent]
        return self._json("POST", API + "/files", meta)["id"]

    def folder(self):
        if not self._folder:
            parent = ""
            for n in self.path:
                parent = self._child_folder(n, parent)
            self._folder = parent
        return self._folder

    def find(self, name):
        q = f"name = '{name}' and '{self.folder()}' in parents and trashed = false"
        d = self._json("GET", API + "/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}))
        return d["files"][0]["id"] if d.get("files") else None

    def upload(self, path, name, mime, file_id=None):
        """Resumable upload in 8 MB chunks (low RAM). Returns file id."""
        size = os.path.getsize(path)
        meta = {"name": name} if file_id else {"name": name, "parents": [self.folder()]}
        url = (UP + "/" + file_id if file_id else UP) + "?uploadType=resumable&fields=id"
        with self._req("PATCH" if file_id else "POST", url, json.dumps(meta).encode(),
                       {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Type": mime,
                        "X-Upload-Content-Length": str(size)}) as r:
            loc = r.headers["Location"]
        chunk = 8 * 1024 * 1024
        with open(path, "rb") as f:
            off = 0
            while True:
                data = f.read(chunk)
                end = off + len(data) - 1
                try:
                    with urllib.request.urlopen(urllib.request.Request(loc, data=data, method="PUT", headers={
                            "Content-Range": f"bytes {off}-{end}/{size}" if data else f"bytes */{size}"}), timeout=120) as r:
                        return json.loads(r.read())["id"]
                except urllib.error.HTTPError as e:
                    if e.code != 308:
                        raise
                off = end + 1

    def put_json(self, name, obj):
        p = Path("/tmp") / f"_gd_{os.getpid()}_{threading.get_ident()}_{name}"
        p.write_text(json.dumps(obj))
        try:
            return self.upload(str(p), name, "application/json", self.find(name))
        finally:
            p.unlink(missing_ok=True)

    def get_json(self, name):
        fid = self.find(name)
        if not fid:
            return None
        with self._req("GET", f"{API}/files/{fid}?alt=media") as r:
            return json.loads(r.read())

    def open_media(self, fid, rng=None):
        return self._req("GET", f"{API}/files/{fid}?alt=media", headers={"Range": rng} if rng else None, timeout=120)

    def download(self, fid, dest):
        with self.open_media(fid) as r, open(dest, "wb") as f:
            while b := r.read(1 << 20):
                f.write(b)


# ---------- token vault (encrypted, persisted in the admin Drive) ----------
def _fernet():
    from cryptography.fernet import Fernet
    seed = os.environ.get("TOKEN_ENC_KEY") or os.environ.get("SESSION_SECRET") or ""
    if not seed:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("mps-drive-tokens:" + seed).encode()).digest()))


_admin = {"acct": None}
_users = {}          # username -> {"enc": ciphertext, "email": str, "since": ts}
_accts = {}          # username -> Account (connected users) / "~"+username -> admin subfolder Account
_vlock = threading.Lock()


def admin():
    if _admin["acct"] is None and configured() and os.environ.get("GDRIVE_REFRESH_TOKEN"):
        _admin["acct"] = Account(os.environ["GDRIVE_REFRESH_TOKEN"], [folder_name()])
    return _admin["acct"]


def set_admin_refresh(rt):
    _admin["acct"] = Account(rt, [folder_name()])
    if _users:  # copy the existing user Drive links into the admin Drive vault right away
        try:
            with _vlock:
                _save_vault()
        except Exception:  # noqa: BLE001
            pass


def enabled():
    return configured() and admin() is not None


def user_connected(u):
    return configured() and u in _users and _fernet() is not None


def user_email(u):
    return (_users.get(u) or {}).get("email", "")


def _decrypt(u):
    f = _fernet()
    return f.decrypt(_users[u]["enc"].encode()).decode() if f and u in _users else ""


def account_for(owner):
    """owner is a username, or ADMIN for the admin root folder. Returns an Account or None."""
    if not configured():
        return None
    if owner == ADMIN:
        return admin()
    if user_connected(owner):
        a = _accts.get(owner)
        if a is None:
            a = _accts[owner] = Account(_decrypt(owner), [folder_name()])
        return a
    if admin() is None:
        return None
    k = "~" + owner
    if k not in _accts:
        _accts[k] = Account(admin().refresh, [folder_name(), "users", owner])
    return _accts[k]


def target_for(user):
    """Where new files for this user go: (owner_key, Account) or (None, None) for local only."""
    if user_connected(user):
        return user, account_for(user)
    if admin() is not None:
        return "~" + user, account_by_key("~" + user)
    return None, None


def account_by_key(key):
    if not key or key == ADMIN:
        return admin()
    if key.startswith("~"):
        u = key[1:]
        if admin() is None:
            return None
        k = "~" + u
        if k not in _accts:
            _accts[k] = Account(admin().refresh, [folder_name(), "users", u])
        return _accts[k]
    return account_for(key) if user_connected(key) else None


def _save_vault():
    data = {"v": 1, "users": _users}
    try:
        LOCAL_TOKENS.write_text(json.dumps(data)); os.chmod(LOCAL_TOKENS, 0o600)
    except Exception:  # noqa: BLE001
        pass
    a = admin()
    if a:
        a.put_json(TOKENS_FILE, data)


def load_vault():
    """Called on startup: prefer the copy in the admin Drive (survives disk wipes), else the local cache."""
    data = None
    a = admin()
    if a:
        try:
            data = a.get_json(TOKENS_FILE)
        except Exception as e:  # noqa: BLE001
            print("token vault load failed:", type(e).__name__, flush=True)
    if data is None and LOCAL_TOKENS.exists():
        try:
            data = json.loads(LOCAL_TOKENS.read_text())
        except Exception:  # noqa: BLE001
            data = None
    f = _fernet()
    for u, rec in ((data or {}).get("users") or {}).items():
        try:
            if f:
                f.decrypt(rec["enc"].encode())
            _users[u] = rec
        except Exception:  # noqa: BLE001  wrong key: ignore that entry
            print("token vault: could not decrypt one entry (key changed?)", flush=True)
    return len(_users)


def connect_user(u, refresh, email=""):
    f = _fernet()
    if not f:
        raise RuntimeError("Set SESSION_SECRET or TOKEN_ENC_KEY on the server first.")
    with _vlock:
        old = _users.get(u)
        _users[u] = {"enc": f.encrypt(refresh.encode()).decode(), "email": email, "since": int(time.time())}
        _accts.pop(u, None)
        _save_vault()
    return old


def disconnect_user(u):
    with _vlock:
        if u not in _users:
            return False
        tok = _decrypt(u)
        _users.pop(u, None)
        _accts.pop(u, None)
        _save_vault()
    if tok:
        revoke(tok)
    return True


def whoami(access_token):
    try:
        req = urllib.request.Request(API + "/about?fields=user(emailAddress)", headers={"Authorization": "Bearer " + access_token})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("user", {}).get("emailAddress", "")
    except Exception:  # noqa: BLE001
        return ""


class Uploader:
    """Background queue: upload each finished video (+thumbnail) to its owner's Drive with retries, then save history."""

    def __init__(self, jobs, save_local):
        self.jobs, self.save_local, self.q = jobs, save_local, queue.Queue()
        self._dirty = set()
        self._ev = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._hist, daemon=True).start()

    def push(self, jid):
        j = self.jobs.get(jid)
        if j and target_for(j.get("user") or "")[1] is not None:
            self.q.put(jid)

    def history_changed(self, keys=None):
        for k in (keys or self.all_keys()):
            self._dirty.add(k)
        self._ev.set()

    def all_keys(self):
        ks = {ADMIN} if admin() else set()
        ks |= {(j.get("drive") or {}).get("acct") or ADMIN for j in list(self.jobs.values()) if j.get("drive")}
        ks |= {u for u in _users}
        return {k for k in ks if not k.startswith("~")} | ({ADMIN} if admin() else set())

    def _run(self):
        import subprocess
        while True:
            jid = self.q.get()
            j = self.jobs.get(jid)
            if not j or j.get("drive") or not j.get("video") or not Path(j["video"]).exists():
                continue
            for attempt in range(6):
                try:
                    key, acct = target_for(j.get("user") or "")
                    if acct is None:
                        break
                    vid = acct.upload(j["video"], f"{jid}.mp4", "video/mp4")
                    th = Path(j["video"]).with_suffix(".jpg")
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", j["video"], "-frames:v", "1",
                                    "-vf", "scale=360:-2", str(th)], capture_output=True, timeout=60)
                    tid = acct.upload(str(th), f"{jid}.jpg", "image/jpeg") if th.exists() else None
                    th.unlink(missing_ok=True)
                    j["drive"] = {"id": vid, "thumb": tid, "acct": key}
                    self.save_local()
                    self.history_changed({key if not key.startswith("~") else ADMIN})
                    break
                except Exception as e:  # noqa: BLE001
                    print("drive upload retry", attempt + 1, type(e).__name__, flush=True)
                    time.sleep(min(300, 15 * 2 ** attempt))

    def _hist(self):
        while True:
            self._ev.wait(); time.sleep(3); self._ev.clear()
            keys, self._dirty = self._dirty, set()
            for k in keys:
                acct = account_by_key(k)
                if acct is None:
                    continue
                for attempt in range(4):
                    try:
                        acct.put_json("history.json", self.snapshot(k))
                        break
                    except Exception as e:  # noqa: BLE001
                        print("drive history retry", type(e).__name__, flush=True)
                        time.sleep(20 * (attempt + 1))

    snapshot = staticmethod(lambda key: {})
