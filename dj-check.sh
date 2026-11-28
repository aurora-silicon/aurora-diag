#!/bin/sh
# SPDX-License-Identifier: MIT
# One command for a tester's Mac (Fedora, Arch Linux ARM or NixOS):
#   curl -fsSL https://raw.githubusercontent.com/aurora-silicon/aurora-diag/main/dj-check.sh | sh
# Runs `aurora-diag dj-check` as root (sudo asks for your password): root
# downloads aurora-diag into a new directory only root can write, runs it
# from there and removes it again. dj-check only reads; the report lands in
# your home directory.
set -eu
REF=${AURORA_DIAG_REF:-main}
case $REF in
  "" | -* | *[!A-Za-z0-9._/-]* | *..*)
    echo "dj-check: AURORA_DIAG_REF is not a branch, tag or commit name: $REF" >&2
    exit 2 ;;
esac
if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v nix-shell >/dev/null 2>&1; then
  # NixOS without a python3 in the system profile
  PY=$(nix-shell -p python3 --run 'command -v python3')
else
  echo "dj-check needs python3: install it (sudo pacman -S python, or sudo dnf install python3) and run this again" >&2
  exit 2
fi
SUDO=; [ "$(id -u)" = 0 ] || SUDO=sudo
# Everything from the download on runs as root, so no process of yours can
# change the files root then runs.
$SUDO sh -eu -c '
REF=$1 PY=$2
shift 2
T=$(mktemp -d /tmp/aurora-diag.XXXXXX)
trap "rm -rf \"\$T\"" EXIT
trap "exit 130" INT TERM
URL="https://github.com/aurora-silicon/aurora-diag/archive/$REF.tar.gz"
if command -v curl >/dev/null 2>&1; then curl -fsSL "$URL" | tar -xz --no-same-owner -C "$T" --strip-components=1
else wget -qO- "$URL" | tar -xz --no-same-owner -C "$T" --strip-components=1; fi
"$PY" -I "$T/bin/aurora-diag" dj-check "$@"
' dj-check "$REF" "$PY" "$@"
