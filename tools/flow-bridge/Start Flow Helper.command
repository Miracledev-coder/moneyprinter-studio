#!/bin/sh
# Double-click me. First run: signs you in to Google Flow once and pairs with your Studio account.
export PATH="$HOME/.local/bin:$PATH"
STUDIO="${STUDIO_URL:-https://moneyprinter-studio.onrender.com}"
cd "$HOME"
if ! command -v flow-bridge >/dev/null 2>&1; then
  echo "Installing the Flow helper first…"; curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh
fi
if ! gflow auth status >/dev/null 2>&1; then
  echo "Step 1: sign in to Google Flow (once). A Chrome window will open."; flow-bridge --signin
fi
if [ ! -f "$HOME/.flow-bridge/key" ]; then
  echo "Step 2: in Studio open Account → 'Connect my Google Flow' → Get pairing code, then paste it here."
  flow-bridge --connect "$STUDIO" --pair --folder "$HOME/Movies/FlowBridge"
else
  flow-bridge --connect "$STUDIO" --folder "$HOME/Movies/FlowBridge"
fi
echo; echo "Helper stopped. You can close this window."; read -r _
