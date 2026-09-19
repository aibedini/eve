#!/usr/bin/env python3
"""Fail when a database, backup, key, or secret file is tracked by git.

Phase 2 guard: this repository previously committed instance/servers.db,
instance/backups/*.db and uploaded receipt images. .gitignore blocks new
ones for ordinary `git add`, but `git add -f` (or a tool) can still slip a
file in. This check is the enforcement point in CI, and it also scans the
range of a pushed change so an add-then-delete cannot leak silently.
"""
import argparse
import re
import subprocess
import sys

FORBIDDEN_SUFFIXES = (
    '.db', '.sqlite', '.sqlite3', '.dump', '.pem', '.key', '.p12',
    '.pfx', '.jks', '.keystore', '.eveenc',
)
FORBIDDEN_NAMES = {'id_rsa', 'id_ed25519', 'credentials', '.netrc'}
FORBIDDEN_DIR_PREFIXES = ('instance/', 'runtime/')
ALLOWED_EXCEPTIONS = {'.env.docker.example'}
BACKUP_SUFFIX_RE = re.compile(r'\.(bak|backup)(\.|$)', re.I)
#: macOS AppleDouble resource forks, e.g. ``._route_smoke.py``. They are filesystem
#: metadata, not source. One was tracked next to the real test it duplicated, and because
#: its name is not ``test_*.py`` unittest discovery never ran it - the route contract it
#: held was silently dead until it was moved to tests/test_memory_routes.py.
APPLEDOUBLE_PREFIX = '._'


def is_forbidden(path: str) -> bool:
    """Return True when a repository path must never be committed."""
    normalized = str(path or '').replace('\\', '/').strip()
    while normalized.startswith('./'):
        normalized = normalized[2:]
    if not normalized:
        return False
    name = normalized.rsplit('/', 1)[-1]
    if normalized in ALLOWED_EXCEPTIONS or name in ALLOWED_EXCEPTIONS:
        return False
    lower = normalized.lower()
    if lower.endswith(FORBIDDEN_SUFFIXES):
        return True
    if name in FORBIDDEN_NAMES or name.startswith('.env'):
        return True
    if name.startswith(APPLEDOUBLE_PREFIX):
        return True
    if any(normalized.startswith(prefix) for prefix in FORBIDDEN_DIR_PREFIXES):
        return True
    if BACKUP_SUFFIX_RE.search(name):
        return True
    return False


def _git_lines(*args: str) -> list[str]:
    completed = subprocess.run(
        ['git', *args], capture_output=True, check=True,
    )
    return [line for line in completed.stdout.decode('utf-8', 'replace').split('\n') if line.strip()]


def tracked_files() -> list[str]:
    completed = subprocess.run(['git', 'ls-files', '-z'], capture_output=True, check=True)
    return [p for p in completed.stdout.decode('utf-8', 'replace').split('\0') if p]


def changed_files(git_range: str) -> list[str]:
    return _git_lines('diff', '--name-only', '--diff-filter=AM', git_range, '--')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--range', dest='git_range', default=None,
                        help='also check files added/modified in <A>..<B> (git diff range)')
    parser.add_argument('--path', action='append', default=None,
                        help='check these paths instead of the tracked file list')
    args = parser.parse_args(argv)
    checked = args.path if args.path is not None else tracked_files()
    offending = sorted({p for p in checked if is_forbidden(p)})
    if args.git_range and args.path is None:
        offending = sorted(set(offending) | {p for p in changed_files(args.git_range) if is_forbidden(p)})
    if offending:
        print('ERROR: forbidden database/backup/secret artifacts are present:', file=sys.stderr)
        for path in offending:
            print(f'  - {path}', file=sys.stderr)
        print('Remove them (git rm --cached) and rotate any exposed credential.',
              file=sys.stderr)
        return 1
    print(f'OK: {len(checked)} tracked files; no database/backup/secret artifacts.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
