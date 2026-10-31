#!/bin/bash
# MoneyPrinter Studio: Connect Google Flow. Double-click me (first time: right-click > Open).
# Personalised for one Studio account; the pairing link inside works once and expires 30 min after download.
STUDIO="__STUDIO__"; TOKEN="__TOKEN__"
export PATH="$HOME/.local/bin:$PATH"; cd "$HOME" || exit 1
stage(){ curl -fsS -m 10 -H 'Content-Type: application/json' -d "{\"token\":\"$TOKEN\",\"stage\":\"$1\"}" "$STUDIO/api/flow/progress" >/dev/null 2>&1 || true; }
oops(){ osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with title \"Flow Helper\" with icon caution" >/dev/null 2>&1; echo "❌ $1"; read -r -p "Press Return to close." _; exit 1; }
echo "MoneyPrinter Studio · connecting your Google Flow"; echo
stage started
if ! command -v flow-bridge >/dev/null 2>&1 || ! command -v gflow >/dev/null 2>&1; then
  echo "Installing the helper (about 2 minutes, only the first time)…"; stage installing
  curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh \
    || oops "The install did not finish. Check your internet and double-click this file again. If it fails twice, send a screenshot of this window to your Studio admin."
fi
if ! gflow auth status >/dev/null 2>&1; then
  stage signin
  osascript -e 'display dialog "A Chrome window will open. Sign in to the Google account you use for Flow, then close the window when Flow shows your projects." buttons {"OK"} default button 1 with title "Flow Helper"' >/dev/null 2>&1
  flow-bridge --signin || oops "Google Flow sign-in did not finish. Double-click this file again to retry."
fi
ACC=$(gflow auth list --json 2>/dev/null | grep -Eo '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' | head -1)
[ -z "$ACC" ] && ACC=$(osascript -e 'text returned of (display dialog "Which Google account did you sign in to Flow with? (shown on your Studio Account page; you can leave it empty)" default answer "" buttons {"OK"} default button 1 with title "Flow Helper")' 2>/dev/null)
mkdir -p "$HOME/Movies/FlowBridge"
# Auto-start at login (asked once)
PL="$HOME/Library/LaunchAgents/com.moneyprinter.flowhelper.plist"
if [ ! -f "$PL" ] && [ ! -f "$HOME/.flow-bridge/no-autostart" ]; then
  A=$(osascript -e 'button returned of (display dialog "Start the Flow helper automatically when you log in to this Mac?" buttons {"No", "Yes"} default button 2 with title "Flow Helper")' 2>/dev/null)
  if [ "$A" = "Yes" ]; then WANT_AUTO=1; else touch "$HOME/.flow-bridge/no-autostart"; fi
fi
if [ -n "$TOKEN" ] && [ ! -f "$HOME/.flow-bridge/used-$TOKEN" ]; then
  touch "$HOME/.flow-bridge/used-$TOKEN"
  PAIR=(--pair "$TOKEN")
else
  PAIR=()
fi
if [ -n "$WANT_AUTO" ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.moneyprinter.flowhelper</string>
<key>ProgramArguments</key><array><string>$HOME/.local/bin/flow-bridge</string><string>--connect</string><string>$STUDIO</string><string>--folder</string><string>$HOME/Movies/FlowBridge</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$HOME/.flow-bridge/helper.log</string><key>StandardErrorPath</key><string>$HOME/.flow-bridge/helper.log</string>
</dict></plist>
PLIST
fi
if [ -n "$WANT_AUTO" ]; then
  if [ ${#PAIR[@]} -gt 0 ]; then
    stage pairing
    flow-bridge --connect "$STUDIO" "${PAIR[@]}" --account "$ACC" --pair-only || oops "Pairing did not work (the file may be older than 30 minutes or already used). Download a fresh one from Studio > Account."
  fi
  launchctl unload "$PL" 2>/dev/null; launchctl load "$PL"
  echo "✅ Connected. The helper now runs in the background and starts when you log in. You can close this window."
  read -r -t 60 _; exit 0
fi
echo "✅ Keep this window open (you can minimise it) while Studio makes videos."
flow-bridge --connect "$STUDIO" "${PAIR[@]}" --account "$ACC" --folder "$HOME/Movies/FlowBridge"
echo; read -r -p "Helper stopped. Press Return to close." _
