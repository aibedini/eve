#!/usr/bin/env bash
# Build the Linux agent on the Eve host and publish it for install.sh.
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TARGET="x86_64-unknown-linux-musl"
DEST="$APP_ROOT/static/app-files/bnqo/bnqo-agent"

for tool in cargo rustup musl-gcc; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Missing $tool on the Eve server. Install Rust (rustup) and musl-tools, then retry." >&2
        exit 1
    fi
done

rustup target add "$TARGET"
(cd "$APP_ROOT/bnqo" && cargo build --locked --release --target "$TARGET")

SOURCE="$APP_ROOT/bnqo/target/$TARGET/release/bnqo-agent"
TMP="$(mktemp "$DEST.tmp.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
install -m 0755 "$SOURCE" "$TMP"
mv -f "$TMP" "$DEST"
echo "BNQO agent ready: $DEST"
