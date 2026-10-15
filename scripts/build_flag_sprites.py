#!/usr/bin/env python3
"""Build static/flag-data.js — a single JS file mapping country codes to inline SVGs."""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAGS_DIR = os.path.join(ROOT, "static", "flags", "4x3")
OUT_FILE = os.path.join(ROOT, "static", "flag-data.js")

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
WHITESPACE_RE = re.compile(r"\s{2,}")


def minify_svg(raw: str) -> str:
    svg = COMMENT_RE.sub("", raw)
    svg = svg.replace("\n", " ").replace("\r", "")
    svg = WHITESPACE_RE.sub(" ", svg).strip()
    return svg


def main() -> None:
    if not os.path.isdir(FLAGS_DIR):
        print(f"ERROR: {FLAGS_DIR} not found", file=sys.stderr)
        sys.exit(1)

    lookup: dict[str, str] = {}
    for fname in sorted(os.listdir(FLAGS_DIR)):
        if not fname.endswith(".svg"):
            continue
        code = fname[:-4]
        with open(os.path.join(FLAGS_DIR, fname), encoding="utf-8") as f:
            lookup[code] = minify_svg(f.read())

    payload = json.dumps(lookup, separators=(",", ":"), ensure_ascii=False)
    js = f"window.__FLAG_SVG={payload};\n"

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(js)

    print(f"wrote {len(lookup)} flags to {OUT_FILE} ({len(js):,} bytes)")


if __name__ == "__main__":
    main()
