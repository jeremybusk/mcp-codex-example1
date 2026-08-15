#!/bin/sh
set -eu

mkdir -p "$HOME/.config/opencode" "$HOME/.local/share/opencode" "$HOME/.cache/opencode"
if [ ! -f "$OPENCODE_CONFIG" ]; then
  cp /opt/opencode/opencode.json "$OPENCODE_CONFIG"
fi
exec "$@"
