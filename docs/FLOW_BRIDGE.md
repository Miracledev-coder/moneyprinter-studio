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
Veo 3.1 Lite/Fast/Quality → `veo-lite`/`veo-fast`/`veo-quality`, Omni Flash → `omni-flash`.
Override with `GFLOW_MODEL_MAP='{"Nano Banana Pro":"..."}'`.
Character references are sent as `--ref` (image i2i, video r2v). Video durations are 4, 6, 8 or 10 s.

## Limits

* Flow images are free in Flow. **Veo video spends your Flow credits** (your plan's monthly allowance), so Studio defaults
  to images and uses Veo only where the director asks for motion.
* Pacing is 75–120 s between generations, with a daily cap of 20 (`--cap`). The browser must stay **headed**
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
