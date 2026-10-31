# Google Flow bridge: your own Flow account, connected like Drive

Studio's AI visuals come from **your own Google Flow** (Nano Banana Pro images and Veo video). A small helper runs on
your Mac or PC, signed into *your* Flow, and Studio sends it jobs. Studio never sees your Google password.

* **Who gets what.** Only the admin account (miracle) can use the free image models (Pollinations, HF, Gemini). Every other
  user gets AI visuals **only** through their own connected Flow. Without one, the AI option is locked with a
  "Connect your Google Flow to use AI visuals" link to /account, and stock footage and uploads still work. The server
  enforces this: `/api/generate` and `/api/long` return 403 for `visuals=ai`, `/api/scene/ai` and character generation are
  admin-only, and the director falls back to stock for gated users.
* **Per account.** Each pairing gives a bridge key that belongs to one user. Studio stores only its hash. Jobs go only to
  that user's helper, and uploads are accepted only into that user's own scenes. Unlinking deletes the key at once.

## First test, step by step (2019 MacBook Pro, about 10 minutes)

1. Open **Terminal** (Spotlight → "Terminal") and paste this one line:
   `curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh`
   It installs free tools only (uv, gflow-cli and a Chromium engine), needs no admin password, and puts
   **Start Flow Helper** on your Desktop.
2. Double-click **Start Flow Helper** on the Desktop. If macOS blocks it, right-click it → Open → Open.
3. **One-time sign-in.** A Chrome window opens on Google Flow. Sign in with the Google account you use for Flow, click
   "Sign in to Flow" if asked, and close the window once your Flow projects appear. The login stays in a profile on your Mac.
4. In Studio, open **Account → Connect my Google Flow → Get pairing code**. The code is shown once and expires after 10 minutes.
   Paste it into the Terminal window.
5. The Account page now shows **Connected · helper online**, with your Flow account label and when it was last seen.
6. Make a video with **Visuals: AI** (the default). The helper picks up the job, generates each scene in Flow one at a
   time, 75–120 s apart, and uploads every file into the right scene. Keep the window open. Next time, just double-click
   **Start Flow Helper** again.

To unlink, use **Account → Unlink**, or just delete `~/.flow-bridge/key`.

## Routes, as tested (October 2026)

| Route | What it is | Status |
|---|---|---|
| **A/B (default)** | The helper calls [gflow-cli](https://github.com/ffroliva/gflow-cli) 0.92.1 (open source, Playwright). It drives Flow in a real, headed Chrome with your persistent profile. | Wired in. The model aliases were checked against `gflow models --json`, and the command lines were tested with a stub. A real generation needs your sign-in (step 3). |
| C | The VEO Automation extension saves into the watched folder `~/Movies/FlowBridge/flow-XXXX/` and the helper uploads new files. | Works. Use `--manual`. |
| D | Send to Flow → prompt pack (prompts.txt) → you generate in Flow → drag files onto the scenes. | Works, always available. |

**Why gflow-cli rather than our own Playwright selectors:** it already handles Flow's sign-in, reCAPTCHA (it runs headed),
the project API, references and downloads, and it is maintained against Flow's UI. It already knows the new
`flow.google.com` host. A public probe from the box confirmed that `labs.google/fx/tools/flow` now redirects to
`flow.google.com/about`, with a "Sign in" link and a "Create with Google Flow" button. Signed-in selectors can't be
checked without an account. Other projects reviewed: eddie-fqh/flow-py (CDP, video only), GabrielGargiuloDev/google-flow-mcp
(CDP, validated images and Veo end-to-end) and TMSSS05/google-flow-browser-mcp (its video tool stops before generating).
They are kept as fallbacks if gflow-cli stalls.

**Model mapping** (Studio → gflow): Nano Banana Pro → `nano-pro`, Nano Banana 2 → `nano2`, Imagen 4 → `image4`,
Veo 3.1 Lite/Fast/Quality → `veo-lite`/`veo-fast`/`veo-quality`, Omni Flash → `omni-flash` (up to 10 s clips, 7 reference images; Veo is capped at 8 s). Pick it in the Flow video picker on Shorts or Long-form; the choice is saved as your default.
Override with `GFLOW_MODEL_MAP='{"Nano Banana Pro":"..."}'`.
Character references are sent as `--ref` (image i2i, video r2v). Video durations are 4, 6, 8 or 10 s.

## Limits

* Flow images are free in Flow. **Veo video spends your Flow credits** (your plan's monthly allowance), so Studio defaults
  to images and uses Veo only where the director asks for motion.
* Pacing is 75–120 s between generations, with a daily cap of 50 (`--cap`). Studio also accepts at most 50 Flow files per user per day (`CAP_FLOW`). The browser must stay **headed**
  because of reCAPTCHA, so leave the Chrome window alone while it works.
* If Flow changes its page, gflow-cli exits with code 23. The helper then stops automating and prints the manual route,
  and the remaining scenes stay open. Fix it with `uv tool upgrade gflow-cli`.
* Automating Flow is unofficial and may conflict with Google's terms. Use your own account, at your own risk, at a slow pace.

## Commands

    flow-bridge --signin                                     # one-time Flow sign-in
    flow-bridge --connect https://moneyprinter-studio.onrender.com --pair   # pair (asks for the code)
    flow-bridge --connect https://moneyprinter-studio.onrender.com          # stay online (saved key)
    flow-bridge --connect ... --manual                       # route C/D only, never drives Flow
    flow-bridge --job ~/Downloads/flow-xxxx.json             # one-off job file

The key is stored at `~/.flow-bridge/key` (mode 600) and is never printed. A 403 from Studio deletes it and asks you to pair again.

## Credits (Google's official table, read 2026-10-31)
Source: https://support.google.com/flow/answer/16526234. Cost is per generation.
| Model | Credits |
|---|---|
| Veo 3.1 Lite | 10 (Ultra: 5) |
| Veo 3.1 Fast | 20 (Ultra: 10) |
| Veo 3.1 Quality | 100 (8 s only) |
| Veo 3.1 Lite lower priority | 0, Ultra 20x / family managers only |
| Omni Flash 720p | 4 s: 7 · 6 s: 10 · 8 s: 12 · 10 s: 15 (360p is about half) |
| Nano Banana Pro / 2 / 2 Lite images | Not on Google's table; gflow-cli docs say 0 credits, daily image quota |
Every account gets 50 free credits a day; AI Plus adds 200/month, Pro 1,000, Ultra 10,000 or 25,000.
The helper reads your balance with `gflow credits user --json` every 30 min (spends nothing), and the Shorts and Long-form pages show it next to the estimate.

## Flow Agent (optional, beta)
The "Use Flow Agent instructions" checkbox passes the preset look and story bible to each image as a Flow Agent instruction card (`gflow image t2i/i2i -i "…"`). Agent instructions cost no credits; images still count toward the 50/day helper cap and 75–120 s pacing. If an account rejects it, the helper retries that image without it.
