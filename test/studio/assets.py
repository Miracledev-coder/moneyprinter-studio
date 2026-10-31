"""Synthetic inputs for fastrender tests and the timing simulation (no network, no API keys)."""
import os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ff(*a):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *a], check=True)


def make(wd, n=6, secs=20.0, videos=2, size=(1080, 1920)):
    """n scenes (stills from Nano-Banana-like PNGs + a few Veo-like 8 s clips), voice, music, srt, font, watermark, end card."""
    from PIL import Image, ImageDraw
    wd = Path(wd); wd.mkdir(parents=True, exist_ok=True); (wd / "fonts").mkdir(exist_ok=True)
    files, clips = {}, []
    per = secs / n
    for k in range(n):
        if k < videos:
            p = wd / f"scene{k:02d}.mp4"
            ff("-f", "lavfi", "-i", f"testsrc2=s={size[0]}x{size[1]}:r=24:d=8", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(p))
            still = False
        else:
            p = wd / f"scene{k:02d}.png"
            im = Image.new("RGB", size, ((40 * k) % 255, 90, 160))
            d = ImageDraw.Draw(im)
            for y in range(0, size[1], 60):
                d.line((0, y, size[0], y + 200), fill=(255, 255 - y % 255, 80), width=8)
            im.save(p)
            still = True
        fade = k in (1, 3) and k < n - 1
        clips.append({"name": p.name, "dur": round(per + (0.4 if fade else 0), 3), "still": still, "kb": k, "zoom": True, "anim": False, "fade": fade})
        files[p.name] = str(p)
    ff("-f", "lavfi", "-i", f"sine=frequency=220:duration={secs}", "-c:a", "libmp3lame", "-b:a", "48k", str(wd / "voice.mp3"))
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:a", "libmp3lame", "-b:a", "96k", str(wd / "music.mp3"))
    words = "this is a synthetic subtitle line for the render test".split()
    with open(wd / "subtitle.srt", "w") as f:
        t = 0.0
        for i in range(int(secs / 0.5)):
            a, b = t, t + 0.5; t = b
            ts = lambda x: f"00:00:{int(x):02d},{int((x % 1) * 1000):03d}"
            f.write(f"{i + 1}\n{ts(a)} --> {ts(b)}\n{words[i % len(words)]}\n\n")
    font = ROOT / "resource" / "fonts" / "BeVietnamPro-Bold.ttf"
    if font.exists():
        os.link(font, wd / "fonts" / font.name) if not (wd / "fonts" / font.name).exists() else None
        files["fonts/" + font.name] = str(wd / "fonts" / font.name)
    Image.new("RGBA", (300, 120), (255, 255, 255, 200)).save(wd / "wm.png")
    Image.new("RGBA", (720, 1280), (11, 10, 26, 235)).save(wd / "endcard.png")
    for n_ in ("voice.mp3", "music.mp3", "subtitle.srt", "wm.png", "endcard.png"):
        files[n_] = str(wd / n_)
    spec = {"W": 720, "H": 1280, "look": "warm", "xf": 0.4, "clips": clips, "audio": "voice.mp3", "voice_volume": 1.0, "end": 2.5,
            "hd": False, "bgm": "music.mp3", "bgm_volume": 0.32, "duck": True, "fade_in": 1.5, "fade_out": 2.5,
            "sub": {"file": "subtitle.srt", "force_style": "FontName=Be Vietnam Pro,FontSize=13,Alignment=2,BorderStyle=1,Outline=1"},
            "wm": "wm.png", "endcard": "endcard.png"}
    return spec, files


def info(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    import json
    return json.loads(r.stdout)
