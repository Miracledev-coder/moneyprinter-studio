"""Flow scene handling: stall re-queue, helper failure reports, _flow_score robustness, helper watchdog + retry."""
import http.server, importlib.util, json, os, subprocess, sys, threading, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "studio"))
import flowsched as fs  # noqa: E402


def _job(n=4, kinds=None, t=1000.0):
    return {"id": "f" * 32, "filled": [None] * n, "kinds": kinds or ["image"] * n, "models": ["Nano Banana Pro"] * n,
            "texts": [f"line {i}" for i in range(n)], "prompts": [f"prompt {i}" for i in range(n)], "created": t}


# ---------- stall detection + retries ----------
def test_stalled_scene_is_requeued_then_given_up():
    j = fs.init(_job(), 1000.0)
    j["filled"][:3] = [{"id": "a"}, {"id": "b"}, {"id": "c"}]  # scene 4 never arrives (live job d35e7abd)
    assert fs.stalled(j, now=1100.0) == []
    st = fs.stalled(j, now=1181.0)
    assert [i for i, _ in st] == [3]
    assert fs.record_fail(j, 3, st[0][1], now=1181.0) == ("retry", 2)
    assert fs.stalled(j, now=1300.0) == []            # timer restarts at the re-dispatch
    assert fs.record_fail(j, 3, "x", now=1400.0) == ("retry", 3)
    assert fs.record_fail(j, 3, "x", now=1600.0)[0] == "giveup"
    assert fs.pending(j) == [] and "3" in j["gave"]


def test_helper_progress_keeps_slow_scene_alive_but_not_forever():
    j = fs.init(_job(kinds=["video", "image"], n=2), 1000.0)
    work = (j["id"], 1, 1000.0)                        # helper generating scene 1 (a 5-minute Veo clip)
    for t in range(1000, 1300, 20):
        fs.note_progress(j, work, now=float(t))
        assert fs.stalled(j, now=float(t), work=work) == []   # scene 2 waits behind it: not stalled either
    fs.note_progress(j, work, now=1700.0)
    assert [i for i, _ in fs.stalled(j, now=1700.0, work=work)] == [0]  # stuck > SCENE_MAX video


def test_failure_report_retries_at_once_and_ignores_stale_or_filled():
    j = fs.init(_job(), 1000.0)
    assert fs.record_fail(j, 1, "helper: timed out", now=1010.0, attempt=1) == ("retry", 2)
    assert fs.record_fail(j, 1, "late duplicate", now=1011.0, attempt=1) == ("stale", 2)
    assert fs.att(j, 1) == 2 and j["why"]["1"] == "helper: timed out"
    j["filled"][2] = {"id": "z"}
    assert fs.record_fail(j, 2, "x", attempt=1) == ("done", 0)


# ---------- _flow_score TypeError ----------
def _video(tmp_path):
    p = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x568:d=2", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def test_old_score_crashed_with_typeerror_on_half_written_job():
    """Reproduction: the score thread started before the upload's save and _fload() read a half-written file -> None."""
    j = None
    with pytest.raises(TypeError):
        j["texts"][0]  # what the old _flow_score did with that None


def test_score_survives_missing_job_fields_and_extracts_video_frame(tmp_path):
    logs, seen = [], []

    def verify(img, line, prompt, gem):
        seen.append((img[:2], line, prompt)); return (8, "ok", "")
    p = _video(tmp_path)
    assert fs.score(None, 0, p, verify, None, log=logs.append) is None and "job missing" in logs[-1]
    assert fs.score({"texts": None}, 3, p, verify, None, log=logs.append) == (8.0, "ok")
    assert seen[-1] == (b"\xff\xd8", "", "")          # JPEG frame from the MP4, empty text guards
    assert fs.score(_job(), 0, None, verify, None, log=logs.append) is None
    assert fs.score(_job(), 0, p, lambda *a: None, None, log=logs.append) == (-1.0, "")

    def boom(*a):
        raise TypeError("bad")
    assert fs.score(_job(), 0, p, boom, None, log=logs.append) is None
    assert "Traceback" in logs[-1] and "TypeError: bad" in logs[-1]


# ---------- server endpoint + score wiring ----------
@pytest.fixture(scope="module")
def srv():
    pytest.importorskip("fastapi"); pytest.importorskip("toml")
    sys.path.insert(0, str(ROOT))
    import server
    return server


def test_scene_fail_endpoint_and_flow_score(srv, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    j = dict(_job(), user="u1", th="x", meta={}, auto=True)
    fs.init(j, time.time()); srv._fsave(j)
    monkeypatch.setattr(srv, "_bridge_user", lambda req: "u1" if req.headers.get("authorization") == "Bearer good" else None)
    c = TestClient(srv.app)
    assert c.post("/api/flow/scene-fail", json={"job": j["id"], "scene": 4, "attempt": 1, "error": "x"}).status_code == 403
    r = c.post("/api/flow/scene-fail", headers={"Authorization": "Bearer good"},
               json={"job": j["id"], "scene": 4, "attempt": 1, "error": "gflow exit 1"}).json()
    assert r["action"] == "retry" and r["attempt"] == 2
    assert srv._fload(j["id"])["att"]["3"] == 2
    # _flow_score on a vanished job must not raise (it used to log 'score failed TypeError')
    srv._flow_score("0" * 32, 0, "u1", "nope")


def test_flow_runner_requeues_and_falls_back_with_reason(srv, monkeypatch):
    monkeypatch.setattr(fs, "STALL", 1)
    monkeypatch.setattr(srv, "flow_online", lambda u: True)
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)
    monkeypatch.setenv("FLOW_WAIT", "30")
    clock = [time.time()]
    real = time.time

    def fake_time():
        clock[0] += 0.5; return clock[0]
    monkeypatch.setattr(srv.time, "time", fake_time); monkeypatch.setattr(fs.time, "time", fake_time)
    run = srv.flow_runner("u2", "jobx", {"aspect": "9:16", "flow_images_only": True})
    out = run(["a", "b"], ["pa", "pb"], {})
    assert out == {}
    assert set(run.reasons) == {0, 1} and all("Flow failed 3 attempt" in v for v in run.reasons.values())
    monkeypatch.setattr(srv.time, "time", real)


# ---------- helper ----------
@pytest.fixture()
def fb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("flow_bridge_t", ROOT / "tools" / "flow-bridge" / "flow_bridge.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.setattr(m, "STATE", tmp_path / "state")
    return m


def test_helper_version(fb):
    assert fb.VERSION == "2026.10.31.9"


def test_run_watchdog_kills_silent_hang_and_aborts(fb, tmp_path):
    t = time.time(); r = fb._run([sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path), timeout=3)
    assert r.returncode == 124 and time.time() - t < 15 and "timed out" in r.out
    t = time.time(); r = fb._run([sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path), timeout=60, abort=lambda: True)
    assert r.returncode == 125 and time.time() - t < 15


class _Stub(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or b"{}")
        _Stub.calls.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/api/flow/scene-fail":
            out = {"action": "retry", "attempt": body["attempt"] + 1} if body["attempt"] < 2 else {"action": "giveup", "attempt": 2}
        else:
            out = {"jobs": [{"job": "J1", "scenes": []}]}
        b = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


def test_drive_reports_failure_and_retries_same_model(fb, tmp_path, monkeypatch):
    gl = tmp_path / "gflow.log"
    fake = tmp_path / "bin" / "gflow"; fake.parent.mkdir()
    fake.write_text(f"""#!{sys.executable}
import json, sys
open({str(gl)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:2] == ["auth", "status"]: sys.exit(0)
if a[:2] == ["project", "list"]: print(json.dumps({{"projects": [{{"title": "MoneyPrinter Studio", "project_id": "P1"}}]}})); sys.exit(0)
print("generation failed: policy"); sys.exit(1)
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    _Stub.calls = []
    hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    fb.CTX.update(base=f"http://127.0.0.1:{hs.server_address[1]}", key="K")
    job = {"job": "J1", "aspect": "9:16", "scenes": [{"n": 1, "type": "image", "model": "Nano Banana Pro", "prompt": "p", "attempt": 1}]}
    assert fb.drive(job, tmp_path, 50, pace=(0, 0)) is True
    fails = [b for p, a, b in _Stub.calls if p == "/api/flow/scene-fail"]
    assert [f["attempt"] for f in fails] == [1, 2] and all(a == "Bearer K" for p, a, b in _Stub.calls)
    gens = [json.loads(x) for x in gl.read_text().splitlines() if json.loads(x)[0] == "image"]
    assert len(gens) == 2 and all("nano-pro" in g and "P1" in g for g in gens)  # same model, same project
    hs.shutdown(); fb.CTX.clear()
