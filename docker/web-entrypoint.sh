#!/bin/sh
set -eu

mkdir -p /data
chown 65532:65532 /data
exec gosu 65532:65532 "$@"
