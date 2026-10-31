import json, os, sys, threading, time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "studio"))
sys.path.insert(0, str(HERE))
import fastrender  # noqa: E402
import macrender  # noqa: E402
import assets  # noqa: E402


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    wd = tmp_path_factory.mktemp("fr")
    spec, files = assets.make(wd, n=5, secs=10.0, videos=2)
    return spec, files, wd


def test_fastrender_full_features(inputs, tmp_path):
    spec, files, wd = inputs
    out = tmp_path / "o.mp4"
    r = fastrender.render(spec, str(wd), str(out), workers=3, threads="2")
    i = assets.info(out)
    v = [s for s in i["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (720, 1280)
    assert any(s["codec_type"] == "audio" for s in i["streams"])
    assert abs(float(i["format"]["duration"]) - 12.5) < 0.3  # voice + 2.5 s end card
    assert r["encoder"] == "libx264" and r["scenes"] == 5
    assert not list(Path(wd).glob("seg*.mp4"))  # intermediates cleaned


def test_fastrender_lowmem_same_output(inputs, tmp_path):
    """512 MB server path: single-threaded decoders + piecewise scene join (<= 2 decoders at once)."""
    spec, files, wd = inputs
    out = tmp_path / "o.mp4"
    r = fastrender.render(spec, str(wd), str(out), workers=1, lowmem=True)
    i = assets.info(out)
    v = [s for s in i["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (720, 1280)
    assert abs(float(i["format"]["duration"]) - 12.5) < 0.3
    assert r["scenes"] == 5
    assert not list(Path(wd).glob("seg*.mp4")) and not list(Path(wd).glob("pc*.mp4")) and not (Path(wd) / "pieces.txt").exists()


def test_fastrender_landscape_and_broken_scene(inputs, tmp_path):
    spec, files, wd = inputs
    sp = json.loads(json.dumps(spec)); sp.update(W=1280, H=720)
    sp["clips"][2]["name"] = "missing.png"  # a broken scene must not kill the render
    sp.pop("endcard")
    out = tmp_path / "o.mp4"
    r = fastrender.render(sp, str(wd), str(out), workers=2)
    i = assets.info(out)
    v = [s for s in i["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1280, 720) and r["scenes"] == 4
    assert abs(float(i["format"]["duration"]) - 10.0) < 0.3


def test_fastrender_cancel_kills_ffmpeg(inputs, tmp_path):
    spec, files, wd = inputs
    flag = threading.Event()
    threading.Timer(0.8, flag.set).start()
    t = time.time()
    with pytest.raises(fastrender.Cancelled):
        fastrender.render(spec, str(wd), str(tmp_path / "o.mp4"), workers=1, cancel=flag.is_set)
    assert time.time() - t < 5


def test_videotoolbox_falls_back_to_x264(inputs, tmp_path):
    spec, files, wd = inputs
    r = fastrender.render(spec, str(wd), str(tmp_path / "o.mp4"), encoder="h264_videotoolbox", workers=3, log=lambda m: None)
    assert r["encoder"] == "libx264"  # no VideoToolbox on Linux: same output via libx264


# ---------- dispatch / fallback / upload through the real HTTP endpoints ----------

@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    app = FastAPI()
    macrender.install(app, lambda r: "alice" if r.headers.get("authorization") == "Bearer k" else None,
                      lambda r: JSONResponse({"error": "bad bridge key"}, 403))
    monkeypatch.setattr(macrender, "POLL_S", 2)
    macrender._SEEN.clear(); macrender._JOBS.clear(); macrender._POLLING.clear()
    return TestClient(app)


H = {"Authorization": "Bearer k"}


def _dispatch_bg(spec, files, wd, dest, **kw):
    box = {}

    def run():
        try:
            box["r"] = macrender.dispatch("alice", "job12345", spec, files, str(wd), str(dest), **kw)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run); t.start()
    return t, box


def test_offline_helper_falls_back_immediately(inputs, tmp_path):
    spec, files, wd = inputs
    t = time.time()
    with pytest.raises(macrender.Fallback):
        macrender.dispatch("nobody", "j", spec, files, str(tmp_path), str(tmp_path / "o.mp4"))
    assert time.time() - t < 0.2


def test_bad_key_rejected(client):
    assert client.post("/api/render/next", json={}, headers={"Authorization": "Bearer nope"}).status_code == 403


def test_mac_render_roundtrip(client, inputs, tmp_path):
    spec, files, wd = inputs
    assert client.post("/api/render/next?wait=0.1", json={}, headers=H).json()["rid"] is None  # marks the helper online
    dest = tmp_path / "final.mp4"
    th, box = _dispatch_bg(spec, files, tmp_path, dest)
    job = client.post("/api/render/next?wait=2", json={}, headers=H).json()
    assert job["rid"] and job["spec"]["W"] == 720 and {i["name"] for i in job["inputs"]} == set(files)
    assert all(len(i["sha256"]) == 64 for i in job["inputs"])
    hw = tmp_path / "helper"; hw.mkdir()
    for i in job["inputs"]:  # helper downloads every input
        r = client.get(f"/api/render/{job['rid']}/file/{i['name']}", headers=H)
        assert r.status_code == 200 and len(r.content) == i["size"]
        (hw / i["name"]).parent.mkdir(exist_ok=True, parents=True); (hw / i["name"]).write_bytes(r.content)
    assert client.get(f"/api/render/{job['rid']}/file/..%2Fetc%2Fpasswd", headers=H).status_code == 404
    out = hw / "out.mp4"
    fastrender.render(job["spec"], str(hw), str(out), workers=3)
    assert client.post(f"/api/render/{job['rid']}/progress", json={"pct": 50, "msg": "x"}, headers=H).json()["cancel"] is False
    assert client.post(f"/api/render/{job['rid']}/result", content=b"not a video" * 200, headers=H).status_code == 400
    r = client.post(f"/api/render/{job['rid']}/result", content=out.read_bytes(),
                    headers=dict(H, **{"X-Render-Timings": json.dumps({"cut_s": 1.0})}))
    assert r.json()["ok"]
    th.join(10)
    assert box.get("r") == {"cut_s": 1.0} and dest.stat().st_size == out.stat().st_size


def test_unclaimed_render_falls_back(client, inputs, tmp_path):
    spec, files, wd = inputs
    macrender._SEEN["alice"] = time.time()  # looked online, but never claims
    t = time.time()
    with pytest.raises(macrender.Fallback, match="claim"):
        macrender.dispatch("alice", "j", spec, files, str(tmp_path), str(tmp_path / "o.mp4"), claim_s=1.5)
    assert time.time() - t < 4


def test_helper_failure_and_silence_fall_back(client, inputs, tmp_path):
    spec, files, wd = inputs
    macrender._SEEN["alice"] = time.time()
    th, box = _dispatch_bg(spec, files, tmp_path, tmp_path / "o.mp4")
    job = client.post("/api/render/next?wait=2", json={}, headers=H).json()
    client.post(f"/api/render/{job['rid']}/fail", json={"error": "ffmpeg crashed"}, headers=H)
    th.join(5)
    assert isinstance(box.get("e"), macrender.Fallback) and "ffmpeg crashed" in str(box["e"])
    th, box = _dispatch_bg(spec, files, tmp_path, tmp_path / "o.mp4", beat_s=1.5)
    job = client.post("/api/render/next?wait=2", json={}, headers=H).json()
    th.join(6)
    assert isinstance(box.get("e"), macrender.Fallback) and "quiet" in str(box["e"])
    # a late upload from the dead helper is refused
    assert client.post(f"/api/render/{job['rid']}/result", content=b"x", headers=H).status_code == 409


def test_stop_reaches_the_helper(client, inputs, tmp_path):
    spec, files, wd = inputs
    macrender._SEEN["alice"] = time.time()
    stop = threading.Event()
    th, box = _dispatch_bg(spec, files, tmp_path, tmp_path / "o.mp4", cancel=stop.is_set)
    job = client.post("/api/render/next?wait=2", json={}, headers=H).json()
    stop.set()
    time.sleep(0.8)
    assert client.post(f"/api/render/{job['rid']}/progress", json={"pct": 5}, headers=H).json()["cancel"] is True
    th.join(5)
    assert isinstance(box.get("e"), fastrender.Cancelled)
