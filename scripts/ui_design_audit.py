#!/usr/bin/env python3
"""Audit the templates for design-system drift and keep the drift from growing.

The design-system contract lives in `.agents/skills/eve-ui/SKILL.md` and
`docs/UI_DESIGN_SYSTEM.md`. The templates predate the contract, so instead of
pretending they are clean this tool counts the violations per file and compares
them with a checked-in baseline (`tests/ui_design_baseline.json`). A file may
never exceed its baseline, so every new change is held to the contract while the
existing backlog shrinks at its own pace.

Usage:
    python scripts/ui_design_audit.py                  # report, worst file first
    python scripts/ui_design_audit.py --check          # exit 1 on any regression
    python scripts/ui_design_audit.py --write-baseline # after a real cleanup
    python scripts/ui_design_audit.py --fix-colors     # dry-run the token migration
    python scripts/ui_design_audit.py --fix-colors --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")
BASELINE_PATH = os.path.join(REPO_ROOT, "tests", "ui_design_baseline.json")

# --------------------------------------------------------------------------
# Ratchet rules: name -> (human description, compiled pattern)
# --------------------------------------------------------------------------
INLINE_STYLE = re.compile(r'style="([^"]*)"')
INLINE_HEX = re.compile(r"style=\"[^\"]*#[0-9a-fA-F]{3,8}")
INLINE_INK = re.compile(r'style="[^"]*(?:color:|font-size:)')
DISPLAY_TOGGLE = re.compile(r"\.style\.display")
CHECKBOX = re.compile(r'type="checkbox"')
CHECKBOX_COMPONENT = re.compile(r"checkbox-label|checkmark|toggle-switch|form-toggle-item")
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2705\u274C\u26A0]")

RULES = {
    "inline_styles": ("style=\"...\" attributes (the stylesheet should carry the style)", INLINE_STYLE),
    "hardcoded_colors": ("hex colour inside an inline style", INLINE_HEX),
    "inline_text_ink": ("inline color:/font-size: on text", INLINE_INK),
    "style_display": ("el.style.display toggling (.hidden is the project way)", DISPLAY_TOGGLE),
    "bare_checkbox": ("input[type=checkbox] without the project checkbox/toggle markup", CHECKBOX),
    "emoji": ("emoji used as UI chrome", EMOJI),
}

# --------------------------------------------------------------------------
# Safe, property-aware migration of inline colours to the :root tokens.
#
# Only mappings whose value is identical in the dark theme are listed. The light
# theme re-defines the surface/text tokens, so mapping those is an improvement;
# the state colours (success/warning/danger/primary) are identical in both
# themes. Surface tokens are never mapped onto a text `color:` (that would invert
# the text in the light theme and make it invisible).
# --------------------------------------------------------------------------
STATE_TOKENS = {
    "#ef4444": "--danger",
    "#f59e0b": "--warning",
    "#22c55e": "--success",
    "#6366f1": "--primary",
}
TEXT_TOKENS = {
    "#94a3b8": "--text-secondary",
    "#64748b": "--secondary",
}
SURFACE_TOKENS = {
    "#1e293b": "--bg-card",
}
BORDER_TOKENS = {
    "#334155": "--border-color",
}
SKIP = {"#fff", "#ffffff", "#000", "#000000"}

# A permanently dark surface (a dialog drawn with a white hairline border, a code
# block, a fixed navy panel) must keep its fixed colours: mapping its text to
# var(--text-secondary) would turn the text dark in the light theme and the panel
# would become unreadable. Inside such a region only the state colours, which are
# identical in both themes, are migrated.
FIXED_DARK = re.compile(
    r"rgba\(\s*255\s*,\s*255\s*,\s*255|#f1f5f9|#e5e7eb|#0f172a|#0d0d1a|#1e1e2e|#111827|#0a0a0f"
)


def _mapping_for(prop: str, allow_theme_tokens: bool = True) -> dict[str, str]:
    prop = prop.strip().lower()
    theme = {**TEXT_TOKENS} if allow_theme_tokens else {}
    if prop == "color":
        return {**STATE_TOKENS, **theme}
    if prop in ("background", "background-color"):
        return {**STATE_TOKENS, **(SURFACE_TOKENS if allow_theme_tokens else {})}
    if prop.startswith("border"):
        return {**STATE_TOKENS, **(BORDER_TOKENS if allow_theme_tokens else {})}
    return {}


DECLARATION = re.compile(r"(?P<prop>[a-z-]+)\s*:\s*(?P<value>[^;\"']*)")


def tokenize_inline_style(style: str, allow_theme_tokens: bool = True) -> tuple[str, list[str], list[str]]:
    """Return (new_style, changes, skipped) for one inline style string."""
    changes: list[str] = []
    skipped: list[str] = []

    def replace_declaration(match: re.Match) -> str:
        prop, value = match.group("prop"), match.group("value")
        mapping = _mapping_for(prop, allow_theme_tokens)
        if not mapping:
            return match.group(0)

        def replace_hex(hex_match: re.Match) -> str:
            found = "#" + hex_match.group(1).lower()
            if found in SKIP:
                return hex_match.group(0)
            if found not in mapping:
                if found in {**STATE_TOKENS, **TEXT_TOKENS, **SURFACE_TOKENS, **BORDER_TOKENS}:
                    skipped.append("%s: %s (fixed dark surface)" % (prop, found))
                return hex_match.group(0)
            token = mapping[found]
            changes.append("%s: %s -> var(%s)" % (prop, found, token))
            return "var(%s)" % token

        new_value = re.sub(r"#([0-9a-fA-F]{3,8})", replace_hex, value)
        # An existing "var(--token, #hex)" fallback becomes "var(--token, var(--token))"
        # once the fallback is migrated; collapse that back to the bare token.
        collapsed = re.sub(r"var\((--[a-z-]+)\s*,\s*var\(\1\)\)", r"var(\1)", new_value)
        if collapsed != new_value:
            changes.append("%s: redundant var() fallback collapsed" % prop)
            new_value = collapsed
        return "%s: %s" % (prop, new_value) if new_value != value else match.group(0)

    return DECLARATION.sub(replace_declaration, style), changes, skipped


def remaining_token_migrations(text: str) -> int:
    """How many inline colours in this template the safe migration would still rewrite.

    Used by the guard test to prove the migration has been applied everywhere it
    safely can: any value left is either a fixed-dark-surface colour or a shade
    that is not a token (those are recorded in the ratchet baseline instead).
    """
    total = 0

    def count(match: re.Match) -> str:
        nonlocal total
        window = text[max(0, match.start() - 600):match.end() + 600]
        allow_theme = FIXED_DARK.search(window) is None
        _, changes, _ = tokenize_inline_style(match.group(1), allow_theme)
        total += len(changes)
        return match.group(0)

    INLINE_STYLE.sub(count, text)
    return total


def scan_templates() -> dict[str, dict[str, int]]:
    """Count every ratchet rule per template file."""
    result: dict[str, dict[str, int]] = {}
    for name in sorted(os.listdir(TEMPLATES_DIR)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(TEMPLATES_DIR, name)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        counts: dict[str, int] = {}
        for rule, (_, pattern) in RULES.items():
            if rule == "bare_checkbox":
                found = 0
                for match in CHECKBOX.finditer(text):
                    window = text[max(0, match.start() - 260):match.start()]
                    if not CHECKBOX_COMPONENT.search(window):
                        found += 1
                counts[rule] = found
            else:
                counts[rule] = len(pattern.findall(text))
        if any(counts.values()):
            result[name] = counts
    return result


def load_baseline() -> dict:
    if not os.path.isfile(BASELINE_PATH):
        return {}
    with open(BASELINE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def write_baseline(counts: dict[str, dict[str, int]]) -> None:
    payload = {"rules": {name: desc for name, (desc, _) in RULES.items()}, "templates": counts}
    with open(BASELINE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def check(counts: dict[str, dict[str, int]]) -> list[str]:
    baseline = load_baseline().get("templates", {})
    problems: list[str] = []
    for name, per_rule in sorted(counts.items()):
        allowed = baseline.get(name, {})
        for rule, count in sorted(per_rule.items()):
            limit = allowed.get(rule, 0)
            if count > limit:
                problems.append("%s: %s is %d, baseline allows %d"
                                % (name, rule, count, limit))
    return problems


def fix_colors(apply: bool) -> None:
    """Rewrite exact-token inline colours to var(--token)."""
    touched = 0
    total_skipped = 0
    for name in sorted(os.listdir(TEMPLATES_DIR)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(TEMPLATES_DIR, name)
        # newline="" everywhere: the templates are CRLF in the working tree and a
        # text-mode read/write would silently rewrite the whole file as LF.
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
        changes: list[str] = []
        skipped: list[str] = []

        def replace_style(match: re.Match) -> str:
            window = text[max(0, match.start() - 600):match.end() + 600]
            allow_theme = FIXED_DARK.search(window) is None
            new_style, found, left = tokenize_inline_style(match.group(1), allow_theme)
            changes.extend(found)
            skipped.extend(left)
            return 'style="%s"' % new_style

        updated = INLINE_STYLE.sub(replace_style, text)
        if not changes and not skipped:
            continue
        touched += 1
        total_skipped += len(skipped)
        print("%s: %d inline colours -> tokens" % (name, len(changes)))
        for change in changes[:6]:
            print("    %s" % change)
        if len(changes) > 6:
            print("    ... %d more" % (len(changes) - 6))
        for item in skipped[:4]:
            print("    kept (fixed dark surface): %s" % item)
        if apply and changes:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
    print("\n%d template(s) %s; %d colour(s) kept on fixed dark surfaces"
          % (touched, "updated" if apply else "would change", total_skipped))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when a template exceeds the baseline")
    parser.add_argument("--write-baseline", action="store_true",
                        help="record the current counts as the new baseline")
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    parser.add_argument("--fix-colors", action="store_true",
                        help="migrate exact-token inline colours to var(--token)")
    parser.add_argument("--apply", action="store_true", help="write the --fix-colors changes")
    args = parser.parse_args(argv)

    if args.fix_colors:
        fix_colors(args.apply)
        return 0

    counts = scan_templates()
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
        return 0
    if args.write_baseline:
        write_baseline(counts)
        print("baseline written to %s" % os.path.relpath(BASELINE_PATH, REPO_ROOT))
        return 0
    if args.check:
        problems = check(counts)
        for problem in problems:
            print("REGRESSION  %s" % problem)
        if problems:
            print("\n%d design-system regression(s); see docs/UI_DESIGN_SYSTEM.md"
                  % len(problems))
            return 1
        print("no design-system regression against tests/ui_design_baseline.json")
        return 0

    totals: dict[str, int] = {}
    for per_rule in counts.values():
        for rule, count in per_rule.items():
            totals[rule] = totals.get(rule, 0) + count
    print("%-24s %8s  %s" % ("rule", "count", "description"))
    for rule, (description, _) in RULES.items():
        print("%-24s %8d  %s" % (rule, totals.get(rule, 0), description))
    worst = sorted(counts.items(), key=lambda item: -sum(item[1].values()))[:10]
    print("\nworst templates:")
    for name, per_rule in worst:
        print("  %-28s %s" % (name, dict(sorted(per_rule.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
