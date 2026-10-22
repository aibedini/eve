#!/usr/bin/env bash
# Build the Linux agent on the Eve host and publish it for install.sh.
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TARGET="x86_64-unknown-linux-musl"
DEST="$APP_ROOT/static/app-files/bnqo/bnqo-agent"
BUILD_HOME="/var/cache/eve/bnqo-rust"
export CARGO_HOME="$BUILD_HOME/cargo"
export RUSTUP_HOME="$BUILD_HOME/rustup"
export PATH="$CARGO_HOME/bin:$PATH"

if (( EUID != 0 )); then
    echo "Run this build script as root on the Eve server." >&2
    exit 1
fi

if ! command -v curl >/dev/null 2>&1 || ! command -v musl-gcc >/dev/null 2>&1; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "Automatic dependency installation requires apt-get on the Eve server." >&2
        exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl musl-tools
fi

install -d -m 0700 "$CARGO_HOME" "$RUSTUP_HOME"
if [[ ! -x "$CARGO_HOME/bin/cargo" || ! -x "$CARGO_HOME/bin/rustup" ]]; then
    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl -fsSL --retry 3 --proto '=https' --tlsv1.2 https://sh.rustup.rs -o "$installer"
    sh "$installer" -y --profile minimal --default-toolchain stable --no-modify-path
    rm -f "$installer"
    trap - EXIT
fi

"$CARGO_HOME/bin/rustup" target add "$TARGET"
(cd "$APP_ROOT/bnqo" && "$CARGO_HOME/bin/cargo" build --locked --release --target "$TARGET")

SOURCE="$APP_ROOT/bnqo/target/$TARGET/release/bnqo-agent"
TMP="$(mktemp "$DEST.tmp.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
install -m 0755 "$SOURCE" "$TMP"
mv -f "$TMP" "$DEST"
echo "BNQO agent ready: $DEST"
