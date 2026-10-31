#!/bin/sh
# One-line install:  curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh
set -e
mkdir -p "$HOME/.flow-bridge"
curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/flow_bridge.py -o "$HOME/.flow-bridge/flow_bridge.py"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\nexec python3 "$HOME/.flow-bridge/flow_bridge.py" "$@"\n' > "$HOME/.local/bin/flow-bridge"
chmod +x "$HOME/.local/bin/flow-bridge"
grep -q '.local/bin' "$HOME/.zshrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
echo "Installed. Open a NEW Terminal window and run:  flow-bridge --job ~/Downloads/flow-XXXX.json"
