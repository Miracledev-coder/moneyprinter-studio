#!/bin/sh
# MoneyPrinter Studio · Flow helper installer (macOS 10.15+ Intel/Apple Silicon, also Linux). Free tools only.
#   curl -fsSL https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge/install.sh | sh
# Never compile anything (no Xcode / Command Line Tools needed): wheels only, pinned to versions with Intel + Apple Silicon wheels.
export UV_NO_BUILD=1 UV_PYTHON_PREFERENCE=only-managed
fail() { echo; echo "❌ $1"; echo "   Nothing was changed on your Mac apart from ~/.flow-bridge and ~/.local/bin."; echo "   Send a screenshot of this window to Miracle's MoneyPrinter Studio admin."; exit 1; }
RAW=https://raw.githubusercontent.com/Miracledev-coder/moneyprinter-studio/main/tools/flow-bridge
D="$HOME/.flow-bridge"; B="$HOME/.local/bin"; mkdir -p "$D" "$B"
echo "1/4  Installing uv (free Python manager, no admin password needed)…"
command -v uv >/dev/null 2>&1 || [ -x "$B/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | sh || fail "Could not install uv. Check your internet connection and run the installer again."
export PATH="$B:$HOME/.cargo/bin:$PATH"
echo "2/4  Installing gflow-cli (drives Google Flow in your own Chrome)…"
curl -fsSL "$RAW/constraints.txt" -o "$D/constraints.txt" || fail "Could not download the version list. Check your internet connection and try again."
uv tool install --python 3.12 --upgrade --no-build --constraints "$D/constraints.txt" gflow-cli==0.92.1 \
  || fail "Could not install gflow-cli. If it mentions 'developer tools', cancel any Xcode pop-up: it is NOT needed. Then run the installer again."
echo "3/4  Installing the browser engine (one-time, ~150 MB)…"
"$HOME/.local/share/uv/tools/gflow-cli/bin/playwright" install chromium 2>/dev/null || uv tool run --from gflow-cli playwright install chromium || echo "⚠️  Browser engine download failed; gflow will use your installed Google Chrome instead."
echo "4/4  Installing flow-bridge…"
curl -fsSL "$RAW/flow_bridge.py" -o "$D/flow_bridge.py" || fail "Could not download flow-bridge. Check your internet connection and try again."
printf '#!/bin/sh\nexec uv run --no-project --python 3.12 "$HOME/.flow-bridge/flow_bridge.py" "$@"\n' > "$B/flow-bridge"
chmod +x "$B/flow-bridge"
for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do grep -q '.local/bin' "$rc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"; done
if [ "$(uname)" = Darwin ]; then
  curl -fsSL "$RAW/Start%20Flow%20Helper.command" -o "$HOME/Desktop/Start Flow Helper.command" && chmod +x "$HOME/Desktop/Start Flow Helper.command"
  xattr -d com.apple.quarantine "$HOME/Desktop/Start Flow Helper.command" 2>/dev/null || true
fi
echo
echo "Done. Next: double-click 'Start Flow Helper' on your Desktop (or run: flow-bridge --connect https://moneyprinter-studio.onrender.com --pair)."
