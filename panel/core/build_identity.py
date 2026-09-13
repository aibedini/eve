"""Build identity: which build answered this request?

The intermittent Subscription layout bug is a version-skew symptom (see
``docs/performance/STATIC_ASSETS.md``): a stylesheet from one build paired with HTML from
another renders columns collapsed and bare SVGs oversized. Diagnosing that needs one piece of
information that was missing - the build each response came from.

* ``EVE_BUILD_SHA`` is the authoritative source. A deployment stamps it (docker build arg,
  systemd ``Environment=``, CI variable), so every node of one release reports the same value
  and "are all nodes the same build?" becomes a one-line check;
* without it, the git revision of the checkout is used (development, bare-metal git deploys);
* without git, the application version is used, so the header is always present and never
  crashes a response.

Resolution is memoized: the value must not change within a process, otherwise two responses
from the same worker could disagree.
"""
import functools
import os
import subprocess

ENV_VAR = 'EVE_BUILD_SHA'
MAX_LENGTH = 64
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GIT_TIMEOUT_SECONDS = 2


@functools.lru_cache(maxsize=1)
def _from_git():
    try:
        result = subprocess.run(
            ['git', '-C', _REPO_ROOT, 'rev-parse', '--short=12', 'HEAD'],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return (result.stdout or '').strip() or None
    except Exception:
        return None
    return None


def reset_cache() -> None:
    """Forget the memoized identity (tests, and a checkout that just changed)."""
    _from_git.cache_clear()
    _resolve.cache_clear()


@functools.lru_cache(maxsize=8)
def _resolve(default='unknown') -> dict:
    raw = (os.environ.get(ENV_VAR) or '').strip()
    if raw:
        return {'sha': raw[:MAX_LENGTH], 'source': 'env'}
    git = _from_git()
    if git:
        return {'sha': str(git)[:MAX_LENGTH], 'source': 'git'}
    return {'sha': str(default or 'unknown')[:MAX_LENGTH], 'source': 'app_version'}


def resolve_build_sha(default='unknown') -> dict:
    """{'sha': str, 'source': 'env'|'git'|'app_version'} - never raises.

    Resolved once per process: a worker must not report two different builds for two
    responses, and an environment change mid-flight must not move the identity.
    """
    return dict(_resolve(str(default or 'unknown')))


def build_sha(default='unknown') -> str:
    return resolve_build_sha(default)['sha']


def build_headers(default='unknown') -> dict:
    """The response headers that make a build verifiable."""
    resolved = resolve_build_sha(default)
    return {
        'X-Eve-Build': resolved['sha'],
        'X-Eve-Build-Source': resolved['source'],
    }
