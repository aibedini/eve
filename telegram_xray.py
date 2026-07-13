"""Render and supervise loopback-only Xray egress tunnels for Telegram."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


def find_xray_binary(explicit=None):
    candidates = [
        explicit,
        os.environ.get("XRAY_BIN"),
        shutil.which("xray"),
        "/usr/local/bin/xray",
        "/usr/bin/xray",
        "/usr/local/x-ui/bin/xray-linux-amd64",
        "/usr/local/x-ui/bin/xray-linux-arm64",
        "/usr/local/x-ui/bin/xray",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    return None


def _one(query, key, default=""):
    values = query.get(key)
    return values[0] if values else default


def build_xray_config_from_uri(uri: str, local_port: int) -> dict:
    """Build an Xray client config from a common VLESS share URI."""
    parsed = urlparse((uri or "").strip())
    if parsed.scheme.lower() != "vless":
        raise ValueError("Managed Xray currently supports VLESS configurations")
    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ValueError("VLESS configuration is missing UUID, host, or port")
    if not 1024 <= int(local_port) <= 65535:
        raise ValueError("Local SOCKS port must be between 1024 and 65535")

    query = parse_qs(parsed.query, keep_blank_values=True)
    network = _one(query, "type", "tcp").lower()
    if network == "http":
        network = "tcp"
    if network not in ("tcp", "ws", "grpc"):
        raise ValueError(f"Unsupported VLESS transport: {network}")
    security = _one(query, "security", "none").lower()
    if security not in ("none", "tls", "reality"):
        raise ValueError(f"Unsupported VLESS security: {security}")

    user = {
        "id": unquote(parsed.username),
        "encryption": _one(query, "encryption", "none") or "none",
    }
    flow = _one(query, "flow")
    if flow:
        user["flow"] = flow
    stream = {"network": network, "security": security}
    if network == "ws":
        stream["wsSettings"] = {
            "path": _one(query, "path", "/") or "/",
            "headers": {"Host": _one(query, "host", parsed.hostname)},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": _one(query, "serviceName"),
            "multiMode": _one(query, "mode").lower() == "multi",
        }
    if security == "tls":
        tls = {
            "serverName": _one(query, "sni", parsed.hostname),
            "fingerprint": _one(query, "fp", "chrome") or "chrome",
        }
        alpn = [item for item in _one(query, "alpn").split(",") if item]
        if alpn:
            tls["alpn"] = alpn
        stream["tlsSettings"] = tls
    elif security == "reality":
        public_key = _one(query, "pbk")
        if not public_key:
            raise ValueError("REALITY configuration is missing its public key")
        stream["realitySettings"] = {
            "serverName": _one(query, "sni", parsed.hostname),
            "fingerprint": _one(query, "fp", "chrome") or "chrome",
            "publicKey": public_key,
            "shortId": _one(query, "sid"),
            "spiderX": _one(query, "spx", "/") or "/",
        }

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "eve-telegram-socks",
            "listen": "127.0.0.1",
            "port": int(local_port),
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [{
            "tag": "eve-telegram-egress",
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": parsed.hostname,
                "port": int(parsed.port),
                "users": [user],
            }]},
            "streamSettings": stream,
        }],
    }


def write_xray_config(uri: str, local_port: int, directory: str, profile_id: int):
    config = build_xray_config_from_uri(uri, local_port)
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target_dir, 0o700)
    except OSError:
        pass
    target = target_dir / f"profile-{int(profile_id)}.json"
    fd, temporary = tempfile.mkstemp(prefix=".profile-", suffix=".json", dir=str(target_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(target), hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _port_ready(port: int, timeout=0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class XraySupervisor:
    def __init__(self, directory, xray_bin=None):
        self.directory = directory
        self.xray_bin = find_xray_binary(xray_bin)
        self.processes = {}

    def stop(self, profile_id, remove_config=False):
        current = self.processes.pop(int(profile_id), None)
        if current:
            process = current["process"]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if remove_config:
            try:
                os.unlink(Path(self.directory) / f"profile-{int(profile_id)}.json")
            except FileNotFoundError:
                pass

    def sync(self, profile_id, uri, local_port):
        if not self.xray_bin:
            return {"success": False, "state": "runtime_missing",
                    "error": "Xray runtime was not found; configure XRAY_BIN"}
        config_path, digest = write_xray_config(
            uri, int(local_port), self.directory, int(profile_id),
        )
        current = self.processes.get(int(profile_id))
        if current and current["process"].poll() is None and current["digest"] == digest:
            ready = _port_ready(local_port)
            return {"success": ready, "state": "running" if ready else "not_ready",
                    "pid": current["process"].pid,
                    "error": None if ready else "Xray process is running but SOCKS is not ready"}
        self.stop(profile_id)
        validation = subprocess.run(
            [self.xray_bin, "run", "-test", "-config", config_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, timeout=15, check=False, shell=False,
        )
        if validation.returncode != 0:
            return {"success": False, "state": "invalid_config",
                    "error": (validation.stderr or "Xray rejected the configuration")[-500:]}
        process = subprocess.Popen(
            [self.xray_bin, "run", "-config", config_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, shell=False,
        )
        self.processes[int(profile_id)] = {"process": process, "digest": digest}
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return {"success": False, "state": "failed",
                        "error": f"Xray exited with status {process.returncode}"}
            if _port_ready(local_port):
                return {"success": True, "state": "running", "pid": process.pid}
            time.sleep(0.2)
        self.stop(profile_id)
        return {"success": False, "state": "start_timeout",
                "error": "Xray did not open its local SOCKS port in time"}

    def stop_all(self):
        for profile_id in list(self.processes):
            self.stop(profile_id, remove_config=True)

    def cleanup_orphans(self, valid_profile_ids):
        valid = {int(value) for value in valid_profile_ids}
        directory = Path(self.directory)
        if not directory.exists():
            return
        for path in directory.glob("profile-*.json"):
            try:
                profile_id = int(path.stem.split("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if profile_id not in valid:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
