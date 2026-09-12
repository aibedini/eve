"""Machine-checked release readiness (phase 31).

A release is a moment when several invariants must hold at once: the version is
well formed, dependencies are hash-pinned, the security scanners are wired into
CI, the image does not run as root, the disclosure policy exists and no secret
file is tracked. This script checks them all without network access so both the
CI guard and a human can run it in seconds.

Profiles:

* ci (default): everything that must hold on every commit; the changelog may lag
  behind the patch version because release notes are cut on an explicit release;
* release: additionally requires CHANGELOG.md and RELEASE_NOTES.md to name the
  version being released. The Docker tag workflow runs this profile.

Usage:

    python scripts/release_check.py --json
    python scripts/release_check.py --profile release
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_RE = re.compile(r'^APP_VERSION = "(\d+\.\d+\.\d+)"', re.MULTILINE)
CHANGELOG_RE = re.compile(r"^##\s*\[?(\d+\.\d+\.\d+)\]?", re.MULTILINE)
REQUIRED_WORKFLOWS = ("security.yml", "tests.yml", "docker-publish.yml")
REQUIRED_SCANNERS = {
    "codeql": "github/codeql-action",
    "gitleaks": "gitleaks-action",
    "pip-audit": "gh-action-pip-audit",
    "trivy": "trivy",
    "forbidden-artifacts": "forbidden-artifacts",
}
SECRET_NAMES = re.compile(r"^(\.env(\..+)?|id_rsa|id_ed25519)$", re.IGNORECASE)
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3",
                   ".jks")
ALLOWED_SECRET_PATHS = (".env.docker.example",)


def is_secret_path(path) -> bool:
    """True for a file that must never be tracked (keys, databases, env files)."""
    name = os.path.basename(str(path or "").strip()).lower()
    if not name or name in ALLOWED_SECRET_PATHS:
        return False
    if name.endswith(".example") or name.endswith(".sample"):
        return False
    return bool(SECRET_NAMES.match(name)) or name.endswith(SECRET_SUFFIXES)


def _read(root, relative):
    try:
        with open(os.path.join(root, relative), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _check(name, ok, detail, required=True):
    return {"name": name, "ok": bool(ok), "required": bool(required),
            "detail": detail}


def check_app_version(app_source):
    match = VERSION_RE.search(app_source or "")
    if not match:
        return None, _check("app_version", False, "APP_VERSION not found in app.py")
    version = match.group(1)
    return version, _check("app_version", True, "APP_VERSION=%s" % version)


def check_version_docs(version, changelog, release_notes, profile):
    release = profile == "release"
    entries = CHANGELOG_RE.findall(changelog or "")
    newest = entries[0] if entries else None
    notes = CHANGELOG_RE.findall(release_notes or "")
    if newest is None:
        return _check("version_docs", False, "CHANGELOG.md has no version entry",
                      required=release)
    if release:
        ok = newest == version
        detail = "changelog=%s version=%s" % (newest, version)
        if ok and version not in notes:
            ok, detail = False, "RELEASE_NOTES.md does not mention %s" % version
        return _check("version_docs", ok, detail, required=True)
    detail = ("changelog=%s, app=%s (release notes are cut on an explicit release)"
              % (newest, version))
    return _check("version_docs", True, detail, required=False)


def check_lockfile(lock_text):
    entries = 0
    unpinned = []
    current = None
    has_hash = {}
    for line in (lock_text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and "==" in line:
            current = line.split("==", 1)[0].strip()
            entries += 1
            has_hash[current] = False
            continue
        if "--hash=sha256:" in line and current:
            has_hash[current] = True
    unpinned = sorted(name for name, ok in has_hash.items() if not ok)
    if not entries:
        return _check("requirements_lock", False, "requirements.lock has no pinned entries")
    if unpinned:
        return _check("requirements_lock", False,
                      "%d entries without a sha256 hash: %s" % (len(unpinned), unpinned[:5]))
    return _check("requirements_lock", True,
                  "%d hash-pinned entries" % entries)


def check_requirements(requirements_text):
    unpinned = []
    for line in (requirements_text or "").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("-"):
            continue
        if "==" not in text:
            unpinned.append(text.split(";")[0].strip())
    if unpinned:
        return _check("requirements_ranges", True,
                      "%d range(s) resolved by the lock: %s"
                      % (len(unpinned), unpinned[:4]), required=False)
    return _check("requirements_ranges", True, "requirements.txt is fully pinned",
                  required=False)


def check_workflows(workflows):
    missing = [name for name in REQUIRED_WORKFLOWS if name not in workflows]
    if missing:
        return _check("ci_workflows", False, "missing workflows: %s" % missing)
    security = workflows.get("security.yml", "")
    absent = [label for label, needle in REQUIRED_SCANNERS.items()
              if needle not in security]
    if absent:
        return _check("ci_workflows", False, "security.yml is missing: %s" % absent)
    return _check("ci_workflows", True,
                  "%d workflows present; scanners: %s"
                  % (len(workflows), ", ".join(sorted(REQUIRED_SCANNERS))))


def check_dockerfile(text):
    users = re.findall(r"^USER\s+(\S+)", text or "", re.MULTILINE)
    if not users:
        return _check("dockerfile", False, "no USER instruction (image runs as root)")
    if any(user.lower() in ("root", "0") for user in users):
        return _check("dockerfile", False, "USER %s runs as root" % users[-1])
    if "--require-hashes" not in (text or ""):
        return _check("dockerfile", False, "pip install without --require-hashes")
    from_lines = re.findall(r"^FROM\s+(\S+)", text or "", re.MULTILINE)
    if any(image.endswith(":latest") for image in from_lines):
        return _check("dockerfile", False, "base image pinned to :latest")
    digest = all("@" in image for image in from_lines)
    detail = "USER %s, hash-pinned install, base %s" % (users[-1], from_lines)
    return _check("dockerfile", True,
                  detail + ("" if digest else " (base image is tag-pinned, not digest-pinned)"))


def check_security_policy(text):
    lowered = (text or "").lower()
    if not lowered:
        return _check("security_policy", False, "SECURITY.md is missing")
    if "vulnerab" not in lowered or "public issue" not in lowered:
        return _check("security_policy", False,
                      "SECURITY.md does not describe private vulnerability disclosure")
    return _check("security_policy", True, "private disclosure policy present")


def check_tracked_files(root):
    try:
        result = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                                text=True, timeout=60)
    except Exception as exc:
        return _check("tracked_secret_files", True,
                      "git unavailable (%s); skipped" % exc, required=False)
    if result.returncode != 0:
        return _check("tracked_secret_files", True, "git ls-files failed; skipped",
                      required=False)
    offenders = [path.strip() for path in (result.stdout or "").splitlines()
                 if is_secret_path(path)]
    if offenders:
        return _check("tracked_secret_files", False,
                      "secret-like files are tracked: %s" % offenders[:5])
    return _check("tracked_secret_files", True, "no secret or database file is tracked")


def check_attestations(workflow_text):
    text = workflow_text or ""
    missing = [flag for flag in ("sbom: true", "provenance: true") if flag not in text]
    if missing:
        return _check("image_attestations", False,
                      "docker-publish.yml is missing: %s" % missing)
    return _check("image_attestations", True, "SBOM and provenance are built")


def run_checks(root=ROOT, profile="ci"):
    app_source = _read(root, "app.py")
    version, version_check = check_app_version(app_source)
    workflows = {}
    for name in REQUIRED_WORKFLOWS:
        text = _read(root, os.path.join(".github", "workflows", name))
        if text:
            workflows[name] = text
    checks = [
        version_check,
        check_version_docs(version, _read(root, "CHANGELOG.md"),
                           _read(root, "RELEASE_NOTES.md"), profile),
        check_lockfile(_read(root, "requirements.lock")),
        check_requirements(_read(root, "requirements.txt")),
        check_workflows(workflows),
        check_dockerfile(_read(root, "Dockerfile")),
        check_security_policy(_read(root, "SECURITY.md")),
        check_tracked_files(root),
        check_attestations(workflows.get("docker-publish.yml", "")),
    ]
    failed = [item["name"] for item in checks if item["required"] and not item["ok"]]
    return {
        "profile": profile,
        "app_version": version,
        "root": root,
        "ok": not failed,
        "failed": failed,
        "checks": checks,
    }


def format_lines(report):
    lines = ["release check (%s) version=%s" % (report["profile"], report["app_version"])]
    for item in report["checks"]:
        status = "ok" if item["ok"] else ("FAIL" if item["required"] else "note")
        lines.append("  %-22s %-4s %s" % (item["name"], status, item["detail"]))
    lines.append("result: %s" % ("PASS" if report["ok"] else "FAIL"))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eve release readiness check")
    parser.add_argument("--profile", choices=("ci", "release"), default="ci")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=ROOT)
    args = parser.parse_args(argv)
    report = run_checks(args.root, args.profile)
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(*format_lines(report), sep=os.linesep)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
