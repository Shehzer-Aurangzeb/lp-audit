#!/usr/bin/env bash
# Mac/Linux: makes the "lp-audit" command work in any folder. Run once: ./install.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/bin/lp-audit" "$HOME/.local/bin/lp-audit"
chmod +x "$ROOT/bin/lp-audit"
echo "Linked ~/.local/bin/lp-audit."
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH (e.g. in ~/.zshrc): export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac
echo "Next: lp-audit setup"
