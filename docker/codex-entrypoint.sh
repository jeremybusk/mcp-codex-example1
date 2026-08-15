#!/bin/sh
set -eu

mkdir -p "$CODEX_HOME"
if [ ! -f "$CODEX_HOME/config.toml" ]; then
  cp /opt/codex/config.toml "$CODEX_HOME/config.toml"
fi
exec "$@"
