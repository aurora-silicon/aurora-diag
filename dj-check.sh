#!/bin/sh
# SPDX-License-Identifier: MIT
# One command for a tester's Mac (Fedora, Arch Linux ARM or NixOS):
#   curl -fsSL https://raw.githubusercontent.com/aurora-silicon/aurora-diag/main/dj-check.sh | sh
# Downloads aurora-diag into a temporary directory, runs `aurora-diag dj-check`
# as root (sudo asks for your password), and removes the download again.
# dj-check only reads; the report lands in your home directory.
set -eu
REF=${AURORA_DIAG_REF:-main}
URL="https://github.com/aurora-silicon/aurora-diag/archive/$REF.tar.gz"
T=$(mktemp -d "${TMPDIR:-/tmp}/aurora-diag.XXXXXX")
trap 'rm -rf "$T"' EXIT INT TERM
if command -v curl >/dev/null 2>&1; then curl -fsSL "$URL" | tar -xz -C "$T" --strip-components=1
else wget -qO- "$URL" | tar -xz -C "$T" --strip-components=1; fi
chmod 755 "$T"
SUDO=; [ "$(id -u)" = 0 ] || SUDO=sudo
if command -v python3 >/dev/null 2>&1; then
  $SUDO python3 -I "$T/bin/aurora-diag" dj-check "$@"
elif command -v nix-shell >/dev/null 2>&1; then
  # NixOS without a python3 in the system profile
  PY=$(nix-shell -p python3 --run 'command -v python3')
  $SUDO "$PY" -I "$T/bin/aurora-diag" dj-check "$@"
else
  echo "dj-check needs python3: install it (sudo pacman -S python, or sudo dnf install python3) and run this again" >&2
  exit 2
fi
