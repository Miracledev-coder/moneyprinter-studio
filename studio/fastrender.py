"""fastrender: the whole Short compose (clip cutting, crossfades, subtitles, music, voice, watermark, end card) with plain ffmpeg.

Standard library only, so the same file runs on the Render server and on the user's Mac helper (the helper downloads it from
Studio for every render, so both sides always use the same version). One spec (JSON) describes the render; every input is a
file name inside `workdir`.

Encodes: one fast intermediate per scene (in parallel when there are cores to spare) + ONE final encode. The old pipeline
encoded each clip, then the crossfade join, then a MoviePy re-encode, then the final pass.
"""
import json, os, re, signal, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

ENGINE_VERSION = "2026.10.31.1"

GRADE = {"warm": "colorbalance=rs=0.04:gs=0.0:bs=-0.05:rm=0.03:bm=-0.03",
         "cool": "colorbalance=rs=-0.04:bs=0.05:bm=0.03",
         "moody": "colorbalance=rs=0.02:bs=0.03,eq=saturation=0.85:contrast=1.08:brightness=-0.02",
         "neutral": "null"}

KB = [  # Ken Burns moves (zoom expr, x expr, y expr); N = total frames, on = frame index
    ("1.0+0.14*on/N", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                       # slow push in
    ("1.14-0.14*on/N", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                      # pull out
    ("1.12", "(iw-iw/zoom)*on/N", "ih/2-(ih/zoom/2)"),                                # pan left to right
    ("1.12", "(iw-iw/zoom)*(1-on/N)", "ih/2-(ih/zoom/2)"),                            # pan right to left
    ("1.04+0.10*on/N", "iw/2-(iw/zoom/2)", "(ih-ih/zoom)*(1-on/N)"),                  # rise up while pushing in
    ("1.12-0.06*on/N", "(iw-iw/zoom)*0.3*(1+on/N)", "(ih-ih/zoom)*0.6*(1-on/N*0.5)"),  # diagonal drift
]

# intermediate scene files: visually lossless and very cheap to encode; only the final pass decides the delivered quality
INTER = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "16"]


class Cancelled(Exception):
    pass


def probe(ffmpeg, path):
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, timeout=60)
    e = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", e)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", e)
    dur = int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0
    return (int(m[1]), int(m[2]), dur) if m else (0, 0, dur)


def cut_args(src, dur, W, H, look, still=False, zoom=True, kb=0, anim=False, src_dur=0.0):
    """ffmpeg input+filter args for one scene: crop-to-fill, light grade, push-in / Ken Burns, exact duration (same look as director.cut)."""
    grade = "null" if anim else f"eq=contrast=1.06:saturation=1.05,{GRADE.get(look, 'null')}"
    fit = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    frames = max(1, int(dur * 30))
    if still:
        z, x, y = (e.replace("N", str(frames)) for e in KB[kb % len(KB)])
        vf = (f"scale={int(W*1.5)}:{int(H*1.5)}:force_original_aspect_ratio=increase,crop={int(W*1.5)}:{int(H*1.5)},"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps=30,{grade},format=yuv420p")
        if anim:
            vf += (f",rotate='0.008*sin(2*PI*t*1.3)':fillcolor=0xF5F2EA:ow={W}:oh={H}"
                   f",fade=t=in:st=0:d=0.18:color=0xF5F2EA,format=yuv420p")
        return ["-loop", "1", "-i", str(src), "-t", f"{dur:.2f}", "-vf", vf]
    sd = src_dur
    off = 0.4 if sd > dur + 1.0 else 0
    speed = min(1.0, sd / dur) if 0 < sd < dur else 1.0
    loop = ["-stream_loop", "-1"] if speed < 0.75 else []
    pts = f"setpts={1/speed:.3f}*PTS," if 0.75 <= speed < 1.0 else ""
    z = f"scale={int(W*1.06)}:{int(H*1.06)},crop={W}:{H}:x='(iw-ow)/2*(1-t/{dur:.2f})':y='(ih-oh)/2'," if zoom else ""
    vf = f"{pts}{fit},{z}fps=30,{grade},format=yuv420p"
    return [*loop, "-ss", f"{off}", "-i", str(src), "-t", f"{dur:.2f}", "-vf", vf]


def _run(cmd, cancel=None, timeout=1800, procs=None):
    """Run ffmpeg in its own process group so Stop kills it (and only it)."""
    kw = {"start_new_session": True} if os.name != "nt" else {}
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kw)
    if procs is not None:
        procs.add(p)
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True); t.start()
    t0 = time.time()
    try:
        while p.poll() is None:
            if (cancel and cancel()) or time.time() - t0 > timeout:
                kill(p)
                if cancel and cancel():
                    raise Cancelled()
                raise RuntimeError("ffmpeg timed out")
            time.sleep(0.25)
    finally:
        if procs is not None:
            procs.discard(p)
    t.join(5)
    if p.returncode != 0:
        msg = b"".join(err).decode("utf-8", "replace")
        msg = "\n".join(l for l in msg.splitlines() if "ontconfig" not in l)
        raise RuntimeError(f"ffmpeg failed (rc={p.returncode}): {msg[-400:]}")


def kill(p):
    try:
        if os.name != "nt":
            os.killpg(p.pid, signal.SIGKILL)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _esc(path):
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def audio_duration(ffmpeg, path):
    return probe(ffmpeg, path)[2]


def render(spec, workdir, out, ffmpeg="ffmpeg", encoder="libx264", workers=1, threads="1", final_threads=None, cancel=None, lowmem=None,
           progress=None, log=print, procs=None):
    """Render `spec` (files in `workdir`) to `out`. Returns a dict of timings and the encoder actually used."""
    t0 = time.time()
    P = progress or (lambda pct, msg="": None)
    W, H, look, xf = int(spec["W"]), int(spec["H"]), spec.get("look") or "neutral", float(spec.get("xf", 0.4))
    path = lambda n: os.path.join(workdir, n)
    clips = [dict(c) for c in spec["clips"]]
    # 1) one intermediate per scene (parallel)
    done = [0]

    def one(k):
        c = clips[k]
        if cancel and cancel():
            raise Cancelled()
        src = path(c["name"])
        sd = 0.0 if c.get("still") else probe(ffmpeg, src)[2]
        seg = path(f"seg{k:02d}.mp4")
        _run([ffmpeg, "-y", "-loglevel", "error", *cut_args(src, float(c["dur"]), W, H, look, c.get("still", False),
                                                              c.get("zoom", True), int(c.get("kb", 0)), c.get("anim", False), sd),
              "-an", *INTER, "-threads", str(threads), seg], cancel, 600, procs)
        done[0] += 1
        P(5 + 60 * done[0] // len(clips), f"Cutting clips {done[0]}/{len(clips)}")
        return seg

    segs = [None] * len(clips)
    with ThreadPoolExecutor(max(1, int(workers))) as ex:
        futs = {k: ex.submit(one, k) for k in range(len(clips))}
        for k, f in futs.items():
            try:
                segs[k] = f.result()
            except Cancelled:
                raise
            except Exception as e:  # a broken scene must not kill the video: the previous scene holds longer instead
                log(f"fastrender: scene {k + 1} failed ({str(e)[-160:]}); extending the previous scene")
    keep = []
    for k, s in enumerate(segs):
        if s:
            keep.append([s, float(clips[k]["dur"]), bool(clips[k].get("fade"))])
    if not keep:
        raise RuntimeError("no scene could be prepared")
    t_cut = time.time() - t0
    # 2) one final pass: crossfades + subtitles + watermark + end card + voice/music mix
    t1 = time.time()
    adur = audio_duration(ffmpeg, path(spec["audio"]))
    END = float(spec.get("end", 2.5))
    end_on = bool(spec.get("endcard")) and adur > 0
    total = adur + (END if end_on else 0)
    if lowmem is None:
        lowmem = os.environ.get("MPT_LOWMEM_RENDER", "") == "1"
    # lowmem (512 MB servers): every decoder single-threaded (ffmpeg's auto threads = host cores, each with frame
    # buffers) and the scene join done in its own pass, so the final pass decodes ONE video instead of all scenes.
    IT = ["-threads", "1"] if lowmem else []
    temps = []
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    graph, nin = [], 0
    for k, (s, d, _f) in enumerate(keep):
        cmd += [*IT, "-i", s]
        graph.append(f"[{k}:v]fps=30,settb=AVTB,setsar=1,format=yuv420p[n{k}]")
    nin = len(keep)
    acc, t = "[n0]", keep[0][1] + (xf if keep[0][2] else 0)
    for i in range(1, len(keep)):
        d = keep[i][1] + (xf if keep[i][2] else 0)
        lab = f"[j{i}]"
        prev_d = keep[i - 1][1] + (xf if keep[i - 1][2] else 0)
        if keep[i - 1][2] and d > 2 * xf and prev_d > 2 * xf and t - xf > 0:
            graph.append(f"{acc}[n{i}]xfade=transition=fade:duration={xf}:offset={t - xf:.3f}{lab}")
            t = t - xf + d
        else:
            graph.append(f"{acc}[n{i}]concat=n=2:v=1:a=0{lab}")
            t += d
        acc = lab
    if lowmem and len(keep) > 1:
        # same join decisions as the graph above, but built from small pieces (<= 2 decoders at a time) that the
        # concat demuxer then plays back one after another in the final pass
        P(68, "Joining scenes")
        nom = [kp[1] + (xf if kp[2] else 0) for kp in keep]
        fade_at, tt = [False] * len(keep), nom[0]
        for i in range(1, len(keep)):
            if keep[i - 1][2] and nom[i] > 2 * xf and nom[i - 1] > 2 * xf and tt - xf > 0:
                fade_at[i], tt = True, tt - xf + nom[i]
            else:
                tt += nom[i]
        real = [probe(ffmpeg, kp[0])[2] or nom[k] for k, kp in enumerate(keep)]
        norm = "fps=30,settb=AVTB,setsar=1,format=yuv420p"
        pieces = []

        def piece(args, name):
            if cancel and cancel():
                raise Cancelled()
            _run([ffmpeg, "-y", "-loglevel", "error", *args, "-an", *INTER, "-threads", "1", path(name)], cancel, 600, procs)
            pieces.append(path(name))

        for k, kp in enumerate(keep):
            a = xf if fade_at[k] else 0.0
            b = real[k] - (xf if k + 1 < len(keep) and fade_at[k + 1] else 0.0)
            if b - a > 0.04:
                piece([*IT, "-i", kp[0], "-vf", f"trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,{norm}"], f"pc{k:02d}a.mp4")
            if k + 1 < len(keep) and fade_at[k + 1]:
                st = max(0.0, real[k] - xf)
                piece([*IT, "-i", kp[0], *IT, "-i", keep[k + 1][0], "-filter_threads", "1", "-filter_complex",
                       f"[0:v]trim=start={st:.3f},setpts=PTS-STARTPTS,{norm}[x0];[1:v]trim=end={xf:.3f},setpts=PTS-STARTPTS,{norm}[x1];"
                       f"[x0][x1]xfade=transition=fade:duration={xf}:offset=0[v]", "-map", "[v]"], f"pc{k:02d}x.mp4")
        lst = path("pieces.txt")
        with open(lst, "w") as f:
            f.writelines(f"file '{os.path.basename(pc)}'\n" for pc in pieces)
        temps.extend(pieces + [lst])
        cmd = [ffmpeg, "-y", "-loglevel", "error", *IT, "-f", "concat", "-safe", "0", "-i", lst]
        graph, nin, acc = [f"[0:v]{norm}[n0]"], 1, "[n0]"
    ai = nin; cmd += [*IT, "-i", path(spec["audio"])]; nin += 1
    bi = wi = ci = None
    if spec.get("bgm"):
        cmd += ["-stream_loop", "-1", *IT, "-i", path(spec["bgm"])]; bi = nin; nin += 1
    if spec.get("wm"):
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{(adur or 600) + END:.2f}", *IT, "-i", path(spec["wm"])]; wi = nin; nin += 1
    if end_on:
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{total:.2f}", *IT, "-i", path(spec["endcard"])]; ci = nin; nin += 1
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    sub = spec.get("sub")
    if sub and os.path.exists(path(sub["file"])):
        fd = _esc(path("fonts"))
        vf += f",subtitles='{_esc(path(sub['file']))}':fontsdir='{fd}'"
        if sub.get("force_style"):
            vf += f":force_style='{sub['force_style']}'"
    pad = max(0.0, total - t) + 0.5  # video shorter than the voice: hold the last frame (the old MoviePy step looped)
    vf += f",tpad=stop_mode=clone:stop_duration={pad:.2f}"
    graph.append(f"{acc}{vf}[v0]")
    last = "v0"
    if wi is not None:
        ww = max(80, W // 6)
        graph.append(f"[{wi}:v]scale={ww}:-1,format=rgba,colorchannelmixer=aa=0.55[wm]")
        graph.append(f"[{last}][wm]overlay=W-w-{W // 36}:{H // 40}:eof_action=repeat[v1]"); last = "v1"
    if ci is not None:
        graph.append(f"[{last}][{ci}:v]overlay=0:0:eof_action=repeat:enable='gte(t,{adur:.2f})'[v2]"); last = "v2"
    if spec.get("hd"):
        graph.append(f"[{last}]scale={(W * 3) // 2}:{(H * 3) // 2}:flags=lanczos[vh]"); last = "vh"
    vol = float(spec.get("voice_volume") or 1.0)
    apad = f",apad=pad_dur={END}" if end_on else ""
    afo = f",afade=t=out:st={adur + END - 1.2:.2f}:d=1.2" if end_on else ""
    fi, fo = float(spec.get("fade_in", 1.5) or 0), float(spec.get("fade_out", 2.5) or 0)
    mf = (f",afade=t=in:st=0:d={fi:.2f}" if fi > 0 else "") + (f",afade=t=out:st={max(0, total - fo):.2f}:d={fo:.2f}" if fo > 0 else "")
    if bi is not None and spec.get("duck"):
        af = (f"[{ai}:a]volume={vol}{apad},asplit=2[va][sc];[{bi}:a]volume={spec.get('bgm_volume') or 0.3}{mf}[m0];"
              f"[m0][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450[m];"
              f"[va][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{afo}[a]")
    elif bi is not None:
        af = (f"[{ai}:a]volume={vol}{apad}[va];[{bi}:a]volume={spec.get('bgm_volume') or 0.2}{mf}[m];"
              f"[va][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{afo}[a]")
    else:
        af = f"[{ai}:a]volume={vol}{apad}[a]"
    cmd += [*(["-filter_threads", "1"] if lowmem else []), "-filter_complex", ";".join(graph) + ";" + af, "-map", f"[{last}]", "-map", "[a]", "-t", f"{total:.2f}"]
    tail = ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", str(final_threads or threads)]
    if lowmem:
        x264 += ["-x264-params", "rc-lookahead=10:sync-lookahead=0"]
    used = encoder
    P(70, "Final render (subtitles, music, voice)")
    if encoder == "h264_videotoolbox":
        br = "9M" if spec.get("hd") else "5M"  # ~2x what x264 crf 23 spends on a 720p Short: same or better quality
        try:
            _run(cmd + ["-c:v", "h264_videotoolbox", "-b:v", br, "-maxrate", "12M", "-bufsize", "12M", "-profile:v", "high", *tail], cancel, 1800, procs)
        except Cancelled:
            raise
        except Exception as e:
            log(f"fastrender: hardware encoder failed ({str(e)[-160:]}); using libx264")
            used = "libx264"
            _run(cmd + x264 + tail, cancel, 1800, procs)
    else:
        used = "libx264"
        _run(cmd + x264 + tail, cancel, 1800, procs)
    for s in [k_[0] for k_ in keep] + temps:
        try:
            os.remove(s)
        except OSError:
            pass
    P(99, "Rendered")
    return {"cut_s": round(t_cut, 1), "final_s": round(time.time() - t1, 1), "total_s": round(time.time() - t0, 1),
            "encoder": used, "scenes": len(keep)}


if __name__ == "__main__":  # python fastrender.py spec.json workdir out.mp4
    sp = json.load(open(sys.argv[1]))
    print(json.dumps(render(sp, sys.argv[2], sys.argv[3], workers=os.cpu_count() // 2 or 1, threads="auto")))
