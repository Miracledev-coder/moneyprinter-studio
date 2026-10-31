"""Optional Google Drive storage (scope drive.file). Disabled unless GDRIVE_CLIENT_ID/SECRET and a refresh token exist."""
import json, os, queue, threading, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/drive.file"
API = "https://www.googleapis.com/drive/v3"
UP = "https://www.googleapis.com/upload/drive/v3/files"
_tok = {"v": "", "exp": 0}
_state = {"refresh": os.environ.get("GDRIVE_REFRESH_TOKEN", ""), "folder": ""}
_lock = threading.Lock()


def configured():
    return bool(os.environ.get("GDRIVE_CLIENT_ID") and os.environ.get("GDRIVE_CLIENT_SECRET"))


def enabled():
    return configured() and bool(_state["refresh"])


def folder_name():
    return os.environ.get("GDRIVE_FOLDER_NAME") or "MoneyPrinter Studio"


def redirect_uri():
    return (os.environ.get("RENDER_EXTERNAL_URL") or "https://moneyprinter-studio.onrender.com").rstrip("/") + "/oauth/google/callback"


def auth_url(state):
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(dict(
        client_id=os.environ["GDRIVE_CLIENT_ID"], redirect_uri=redirect_uri(), response_type="code", scope=SCOPE,
        access_type="offline", prompt="consent", include_granted_scopes="true", state=state))


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def exchange(code):
    d = _post_form("https://oauth2.googleapis.com/token", dict(
        code=code, client_id=os.environ["GDRIVE_CLIENT_ID"], client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        redirect_uri=redirect_uri(), grant_type="authorization_code"))
    if d.get("refresh_token"):
        _state["refresh"] = d["refresh_token"]
    _tok.update(v=d.get("access_token", ""), exp=time.time() + int(d.get("expires_in", 0)) - 60)
    return d.get("refresh_token", "")


def token():
    with _lock:
        if _tok["v"] and time.time() < _tok["exp"]:
            return _tok["v"]
        d = _post_form("https://oauth2.googleapis.com/token", dict(
            refresh_token=_state["refresh"], client_id=os.environ["GDRIVE_CLIENT_ID"],
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"], grant_type="refresh_token"))
        _tok.update(v=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)) - 60)
        return _tok["v"]


def _req(method, url, body=None, headers=None, timeout=60):
    h = {"Authorization": "Bearer " + token()}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, data=body, method=method, headers=h), timeout=timeout)


def _json(method, url, obj=None):
    with _req(method, url, json.dumps(obj).encode() if obj is not None else None,
              {"Content-Type": "application/json"} if obj is not None else None) as r:
        t = r.read()
        return json.loads(t) if t else {}


def folder():
    if _state["folder"]:
        return _state["folder"]
    q = f"name = '{folder_name()}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    d = _json("GET", API + "/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}))
    if d.get("files"):
        _state["folder"] = d["files"][0]["id"]
    else:
        _state["folder"] = _json("POST", API + "/files", {"name": folder_name(), "mimeType": "application/vnd.google-apps.folder"})["id"]
    return _state["folder"]


def find(name):
    q = f"name = '{name}' and '{folder()}' in parents and trashed = false"
    d = _json("GET", API + "/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}))
    return d["files"][0]["id"] if d.get("files") else None


def upload(path, name, mime, file_id=None):
    """Resumable upload in 8 MB chunks (low RAM). Returns file id."""
    size = os.path.getsize(path)
    meta = {"name": name} if file_id else {"name": name, "parents": [folder()]}
    url = (UP + "/" + file_id if file_id else UP) + "?uploadType=resumable&fields=id"
    with _req("PATCH" if file_id else "POST", url, json.dumps(meta).encode(),
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


def put_json(name, obj):
    p = Path("/tmp") / ("_gd_" + name)
    p.write_text(json.dumps(obj))
    try:
        return upload(str(p), name, "application/json", find(name))
    finally:
        p.unlink(missing_ok=True)


def get_json(name):
    fid = find(name)
    if not fid:
        return None
    with _req("GET", f"{API}/files/{fid}?alt=media") as r:
        return json.loads(r.read())


def open_media(fid, rng=None):
    return _req("GET", f"{API}/files/{fid}?alt=media", headers={"Range": rng} if rng else None, timeout=120)


def download(fid, dest):
    with open_media(fid) as r, open(dest, "wb") as f:
        while b := r.read(1 << 20):
            f.write(b)


class Uploader:
    """Background queue: upload each finished video (+thumbnail) with retries, then save history.json."""

    def __init__(self, jobs, save_local):
        self.jobs, self.save_local, self.q = jobs, save_local, queue.Queue()
        self._dirty = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._hist, daemon=True).start()

    def push(self, jid):
        if enabled():
            self.q.put(jid)

    def history_changed(self):
        if enabled():
            self._dirty.set()

    def _run(self):
        import subprocess
        while True:
            jid = self.q.get()
            j = self.jobs.get(jid)
            if not j or j.get("drive") or not j.get("video") or not Path(j["video"]).exists():
                continue
            for attempt in range(6):
                try:
                    vid = upload(j["video"], f"{jid}.mp4", "video/mp4")
                    th = Path(j["video"]).with_suffix(".jpg")
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", j["video"], "-frames:v", "1",
                                    "-vf", "scale=360:-2", str(th)], capture_output=True, timeout=60)
                    tid = upload(str(th), f"{jid}.jpg", "image/jpeg") if th.exists() else None
                    th.unlink(missing_ok=True)
                    j["drive"] = {"id": vid, "thumb": tid}
                    self.save_local(); self._dirty.set()
                    break
                except Exception as e:  # noqa: BLE001
                    print("drive upload retry", attempt + 1, type(e).__name__, flush=True)
                    time.sleep(min(300, 15 * 2 ** attempt))

    def _hist(self):
        while True:
            self._dirty.wait(); time.sleep(3); self._dirty.clear()
            for attempt in range(4):
                try:
                    put_json("history.json", self.snapshot())
                    break
                except Exception as e:  # noqa: BLE001
                    print("drive history retry", type(e).__name__, flush=True)
                    time.sleep(20 * (attempt + 1))

    snapshot = staticmethod(lambda: {})
