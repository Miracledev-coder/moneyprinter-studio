# Google Flow bridge (Mac)

Google Flow has no public API. The bridge connects MoneyPrinter Studio to **your own** Flow subscription
through your Mac. No Google password is ever asked for or stored, and Studio only ever sees the files you upload.

![Send to Flow panel](flow-send-panel.png)

## What it does

* **Visual engine = Flow** is the default for each user. When your Mac bridge is online, Studio sends every
  scene to Flow: **images with Nano Banana Pro**, **videos with Veo 3.1 - Lite** (or **Omni Flash** if you choose it).
  By default the opening (hook) scene is a video and the rest are images. You can change any scene in the Send to Flow panel.
* If the bridge is offline, not connected, or Flow is slow or rate-limited (no clips within 15 minutes), Studio fills the
  missing scenes with the **free backup models** (FLUX.1-schnell) and shows **"Using backup images"** on the job.
* The director writes one precise prompt per sentence. Each prompt covers the subject and action, the setting and props,
  the camera, the lighting and mood, the style and the aspect ratio. Video prompts also get motion, a camera move and an
  8-second duration. A "story bible" (recurring characters, wardrobe, location, palette) goes into every prompt so the
  scenes look like one film. When each file arrives, Gemini vision scores how well it matches its sentence (0–10).

## One-time setup (about 5 minutes)

1. **Install the free VEO Automation extension** in Chrome: search "VEO Automation Google Flow" in the Chrome Web Store
   (site: veoautomation.com). It batch-runs prompts in Flow and downloads the results.
2. **Install the helper.** Open **Terminal** (press Cmd+Space, type Terminal, press Enter) and paste this line:
   ```
   curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh
   ```
   Close Terminal, then open it again.
3. **Connect.** In Studio go to the **Scene editor → 🎞 Send to Flow → 💻 Connect my Mac**. Copy the line it shows, for example:
   ```
   flow-bridge --connect https://moneyprinter-studio.onrender.com
   ```
   Paste it into Terminal and press Enter. When it asks, paste the **bridge key**. The key is shown only once and
   is never saved on your Mac. If you lose it, click Connect my Mac again to make a new one.
   Leave this Terminal window open while you make videos. The Shorts page shows **● Mac bridge online**.

## Making a video with Flow

1. Make a Short as usual. While the bridge is online, the job says **"Waiting for Google Flow clips 0/7"**.
2. Terminal prints the job's prompts file (`Downloads/flow-XXXXXXXX/prompts.txt`) and the model for each scene.
3. In Flow, open VEO Automation. Pick **Text to Image → Nano Banana Pro** for the image scenes, or
   **Text to Video → Veo 3.1 - Lite** for the video scenes. Click **Upload .txt** and choose that prompts file. Set the
   download folder to `flow-XXXXXXXX` and turn on **auto-rename**, so files start with the scene number. Then press **Run**.
4. Each downloaded file is uploaded to its scene automatically: `✓ 03_….png → scene 3 (3/7)`. When all scenes are in,
   the render carries on by itself.

**Manual route (Scene editor):** click **🎞 Send to Flow**, change models per scene if you want, download the **.txt**
(VEO Automation format: one prompt per paragraph), the **.csv** (prompt in the first column, then scene, type, model,
aspect, duration) or the **job file (.json)**. Then run `flow-bridge --job ~/Downloads/flow-XXXX.json` and paste the
upload token shown in the panel. The panel counts **"Waiting for Flow clips 3/8"** and shows **▶ Render now** once every scene is filled.

## Experimental: let the Mac drive Flow (`--drive`)

`flow-bridge --connect … --drive` uses the open-source **gflow-cli** (github.com/ffroliva/gflow-cli; unofficial, alpha)
to type the prompts into Flow in your own signed-in Chrome. It waits 60–90 seconds between prompts and stops at
20 generations a day (`--cap`). Setup: `uv tool install gflow-cli` then `gflow auth login --browser chrome`.
Automating Google sites may break Google's terms, so use it at your own risk. **The extension route is the default.**

## Model names

Confirmed (Google Flow Help, "Learn about Google Flow models", and a live Flow model list published by google-flow-mcp):

| Use | Flow picker name | Notes |
|---|---|---|
| Images (default) | **Nano Banana Pro** | Others: Nano Banana 2, Nano Banana 2 Lite |
| Videos (default) | **Veo 3.1 - Lite** | 4/6/8 s, cheapest Veo; 9:16 and 16:9 |
| Videos ("Google Omni") | **Omni Flash** (Gemini Omni Flash 1.1) | 4–10 s, 720p or 360p draft |

Not verified: the exact `--model` aliases gflow-cli accepts. The helper tries `nano-banana-pro`, `veo-3-1-lite` and
`omni-flash`. If gflow rejects one, the helper retries with Flow's current default model and says so in Terminal. You can
set your own with `GFLOW_MODEL_MAP='{"Nano Banana Pro":"pro"}'`. VEO Automation's model menu is chosen by hand in the extension.

## Safety

* Upload tokens (per job, valid 7 days) and the bridge key (per user) are stored only as SHA-256 hashes.
* Uploads accept only MP4/MOV/WEBM/JPG/PNG/WEBP files up to 60 MB. Each file must be real media, and it goes only
  into the owner's own job.
* On the free Render plan, jobs and keys reset when the server restarts. If that happens, click Connect my Mac again.
