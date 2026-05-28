#!/bin/bash

set -euo pipefail

OUT="${1:-/root/eve-offline-profile.txt}"

if [ ! -f /etc/os-release ]; then
    echo "Cannot read /etc/os-release" >&2
    exit 1
fi

. /etc/os-release

{
    echo "EVE_OFFLINE_PROFILE_VERSION=1"
    echo "OS_ID=${ID:-}"
    echo "OS_PRETTY=${PRETTY_NAME:-}"
    echo "VERSION_ID=${VERSION_ID:-}"
    echo "VERSION_CODENAME=${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
    echo "ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)"
    echo "KERNEL=$(uname -r)"
    echo "LIBC=$(ldd --version 2>/dev/null | head -1 || true)"
    echo "PYTHON3=$(python3 --version 2>/dev/null || true)"
} | tee "$OUT"

echo
echo "Profile saved to: $OUT"
echo "Send this file/output back to the online build machine, then run:"
echo "  bash prepare-offline-bundle.sh --profile $OUT ."
