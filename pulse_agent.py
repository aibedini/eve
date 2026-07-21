"""Eve Pulse remote vantage agent — runs on a server OUTSIDE Iran.

Managed servers only expose the 3x-ui HTTP API, so instead of eve pushing
probes over SSH, this standalone script is installed manually on a foreign
host (scp it together with pulse.py and telegram_xray.py) and PULLS tasks
from eve / PUSHES results back:

    GET  {eve}/api/pulse/agent/tasks?agent=<name>   (Bearer token)
    POST {eve}/api/pulse/agent/report               (Bearer token)

Dependencies: Python 3.8+ stdlib, requests, pulse.py, telegram_xray.py, and
a local xray binary (same as the local probe engine).
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import time

import requests

import pulse

DEFAULT_POLL_SECONDS = 30
DEFAULT_TIMEOUT = 15
MAX_BACKOFF_SECONDS = 300

# SiteCheck fields accepted from the task profile payload.
_SITE_CHECK_FIELDS = ("name", "url", "expect_substring", "timeout")


def _log(message):
    print(f"[pulse-agent] {message}", flush=True)


def build_profile(payload):
    """Reconstruct a pulse.ProbeProfile from the task's profile dict."""
    payload = payload or {}
    field_names = {f.name for f in dataclasses.fields(pulse.ProbeProfile)}
    kwargs = {}
    for key, value in payload.items():
        if key not in field_names:
            continue
        if key == "site_checks":
            value = [
                pulse.SiteCheck(**{k: v for k, v in (spec or {}).items()
                                   if k in _SITE_CHECK_FIELDS})
                for spec in (value or [])
            ]
        kwargs[key] = value
    return pulse.ProbeProfile(**kwargs)


def fetch_task(http, eve_url, token, agent_name, timeout):
    """Claim one queued remote run; returns the task dict or None."""
    response = http.get(
        f"{eve_url}/api/pulse/agent/tasks",
        params={"agent": agent_name},
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("run_id"):
        return None
    return payload


def run_task(task, probe_runner=None):
    """Probe every config of a claimed task sequentially."""
    probe_runner = probe_runner or pulse.run_probe
    profile = build_profile(task.get("profile"))
    results = []
    configs = task.get("configs") or []
    for index, entry in enumerate(configs, 1):
        config = pulse.PulseConfig(
            uri=entry.get("uri") or "",
            label=entry.get("label") or "",
        )
        _log(f"{index}/{len(configs)} probing {config.label or config.uri[:32]} ...")
        result = probe_runner(config, profile)
        _log(f"{index}/{len(configs)} {result.label}: {result.verdict}"
             + (f" ({result.error})" if result.error else ""))
        results.append(result.to_dict())
    return results


def post_report(http, eve_url, token, run_id, results, timeout):
    """Push probe results back to eve; returns the response payload."""
    response = http.post(
        f"{eve_url}/api/pulse/agent/report",
        json={"run_id": run_id, "results": results},
        headers={"Authorization": f"Bearer {token}"},
        # results for a full-profile run can take a while to upload on a
        # slow uplink; give the read side generous room.
        timeout=(min(timeout, 10), max(timeout, 60)),
    )
    response.raise_for_status()
    return response.json()


def tick(http, eve_url, token, agent_name, timeout, probe_runner=None):
    """One poll cycle: claim a task, run it, report it.

    Returns the run_id that was processed, or None when the queue was empty.
    """
    task = fetch_task(http, eve_url, token, agent_name, timeout)
    if task is None:
        return None
    run_id = task["run_id"]
    _log(f"claimed run #{run_id} ({len(task.get('configs') or [])} config(s))")
    results = run_task(task, probe_runner=probe_runner)
    post_report(http, eve_url, token, run_id, results, timeout)
    _log(f"reported run #{run_id}")
    return run_id


def loop(args, http=None, probe_runner=None):
    """Poll eve forever (or once with --once), backing off when unreachable."""
    http = http or requests.Session()
    failures = 0
    while True:
        try:
            run_id = tick(http, args.eve_url, args.token, args.agent_name,
                          args.timeout, probe_runner=probe_runner)
            failures = 0
            if run_id is None:
                _log("no queued tasks")
        except Exception as exc:
            failures += 1
            _log(f"cycle failed (attempt {failures}): {exc}")
        if args.once:
            return 0 if failures == 0 else 1
        if failures:
            delay = min(args.poll * (2 ** min(failures - 1, 4)), MAX_BACKOFF_SECONDS)
        else:
            delay = args.poll
        time.sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Eve Pulse remote vantage agent (pull tasks, push results)")
    parser.add_argument("--eve-url",
                        default=os.environ.get("PULSE_EVE_URL", ""),
                        help="base URL of the eve panel (env PULSE_EVE_URL)")
    parser.add_argument("--token",
                        default=os.environ.get("PULSE_AGENT_TOKEN", ""),
                        help="agent bearer token (env PULSE_AGENT_TOKEN)")
    parser.add_argument("--agent-name",
                        default=os.environ.get("PULSE_AGENT_NAME", ""),
                        help="agent name as created in the panel (env PULSE_AGENT_NAME)")
    parser.add_argument("--poll", type=int, default=DEFAULT_POLL_SECONDS,
                        help="seconds between polls when idle")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="HTTP timeout (seconds) for eve API calls")
    parser.add_argument("--once", action="store_true",
                        help="run a single poll cycle and exit")
    args = parser.parse_args(argv)

    args.eve_url = args.eve_url.rstrip("/")
    if not args.eve_url or not args.token or not args.agent_name:
        parser.error("--eve-url, --token and --agent-name are required "
                     "(or set PULSE_EVE_URL / PULSE_AGENT_TOKEN / PULSE_AGENT_NAME)")
    _log(f"starting agent '{args.agent_name}' against {args.eve_url}")
    return loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
