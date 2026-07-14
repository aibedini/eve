"""Render and supervise loopback-only Xray egress tunnels for Telegram."""

from __future__ import annotations

import base64
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


def _decode_base64(value: str) -> str:
    raw = unquote(value or "").strip()
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw.encode()).decode()
    except Exception as exc:
        raise ValueError("Configuration contains invalid base64 data") from exc


def _stream_settings(network: str, security: str, values: dict) -> dict:
    aliases = {"raw": "tcp", "websocket": "ws", "http": "tcp"}
    network = aliases.get((network or "tcp").lower(), (network or "tcp").lower())
    if network not in ("tcp", "ws", "grpc"):
        raise ValueError(f"Unsupported Xray transport: {network}")
    security = (security or "none").lower()
    if security not in ("none", "tls", "reality"):
        raise ValueError(f"Unsupported Xray transport security: {security}")

    stream = {"network": network, "security": security}
    if network == "ws":
        stream["wsSettings"] = {
            "path": values.get("path") or "/",
            "headers": {"Host": values.get("host") or values.get("address") or ""},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": values.get("serviceName") or values.get("service_name") or "",
            "multiMode": str(values.get("mode") or "").lower() == "multi",
        }
    if security == "tls":
        tls = {
            "serverName": values.get("sni") or values.get("address") or "",
            "fingerprint": values.get("fp") or "chrome",
        }
        alpn = values.get("alpn") or ""
        if isinstance(alpn, str):
            alpn = [item for item in alpn.split(",") if item]
        if alpn:
            tls["alpn"] = alpn
        stream["tlsSettings"] = tls
    elif security == "reality":
        public_key = values.get("pbk") or values.get("publicKey")
        if not public_key:
            raise ValueError("REALITY configuration is missing its public key")
        stream["realitySettings"] = {
            "serverName": values.get("sni") or values.get("address") or "",
            "fingerprint": values.get("fp") or "chrome",
            "publicKey": public_key,
            "shortId": values.get("sid") or "",
            "spiderX": values.get("spx") or "/",
        }
    return stream


def _base_config(local_port: int, outbound: dict) -> dict:
    if not 1024 <= int(local_port) <= 65535:
        raise ValueError("Local SOCKS port must be between 1024 and 65535")
    outbound["tag"] = "eve-telegram-egress"
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "eve-telegram-socks",
            "listen": "127.0.0.1",
            "port": int(local_port),
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": [outbound],
    }


def build_xray_config_from_uri(uri: str, local_port: int) -> dict:
    """Build a loopback Xray client from VLESS, VMess, Trojan, SS, or WireGuard."""
    parsed = urlparse((uri or "").strip())
    scheme = parsed.scheme.lower()

    if scheme == "vmess":
        payload = json.loads(_decode_base64((uri or "").strip()[8:].split("#", 1)[0]))
        address, port, user_id = payload.get("add"), payload.get("port"), payload.get("id")
        if not address or not port or not user_id:
            raise ValueError("VMess configuration is missing UUID, host, or port")
        values = {
            "address": address, "host": payload.get("host"), "path": payload.get("path"),
            "serviceName": payload.get("serviceName") or payload.get("path"),
            "sni": payload.get("sni"), "alpn": payload.get("alpn"), "fp": payload.get("fp"),
            "pbk": payload.get("pbk"), "sid": payload.get("sid"),
        }
        security = str(payload.get("tls") or "none").lower()
        user = {"id": str(user_id), "alterId": int(payload.get("aid") or 0),
                "security": payload.get("scy") or "auto"}
        outbound = {
            "protocol": "vmess",
            "settings": {"vnext": [{"address": address, "port": int(port), "users": [user]}]},
            "streamSettings": _stream_settings(payload.get("net") or "tcp", security, values),
        }
        return _base_config(local_port, outbound)

    if scheme not in ("vless", "trojan", "ss", "wireguard"):
        raise ValueError("Supported managed protocols are VLESS, VMess, Trojan, Shadowsocks, and WireGuard")

    if scheme == "ss":
        query = parse_qs(parsed.query, keep_blank_values=True)
        username, address, port = parsed.username, parsed.hostname, parsed.port
        if username and address and port:
            credentials = _decode_base64(username)
        else:
            legacy = _decode_base64((uri or "").strip()[5:].split("#", 1)[0].split("?", 1)[0])
            credentials, endpoint = legacy.rsplit("@", 1)
            address, port_text = endpoint.rsplit(":", 1)
            port = int(port_text)
        if ":" not in credentials or not address or not port:
            raise ValueError("Shadowsocks configuration is missing method, password, host, or port")
        method, password = credentials.split(":", 1)
        plugin = unquote(_one(query, "plugin"))
        values = {"address": address}
        network, security = "tcp", "none"
        if plugin:
            parts = [part for part in plugin.split(";") if part]
            plugin_name = parts[0].lower()
            options = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
            if plugin_name == "v2ray-plugin":
                network = "ws"
                security = "tls" if "tls" in parts[1:] else "none"
                values.update({"path": options.get("path"), "host": options.get("host"),
                               "sni": options.get("host")})
            elif plugin_name == "grpc":
                network = "grpc"
                values["serviceName"] = options.get("serviceName")
            else:
                raise ValueError(f"Unsupported Shadowsocks plugin: {plugin_name}")
        outbound = {
            "protocol": "shadowsocks",
            "settings": {"servers": [{"address": address, "port": int(port),
                                        "method": method, "password": password}]},
            "streamSettings": _stream_settings(network, security, values),
        }
        return _base_config(local_port, outbound)

    if not parsed.username or not parsed.hostname or not parsed.port:
        raise ValueError(f"{scheme.upper()} configuration is missing credentials, host, or port")
    query = parse_qs(parsed.query, keep_blank_values=True)
    values = {key: _one(query, key) for key in (
        "path", "host", "serviceName", "mode", "sni", "alpn", "fp", "pbk", "sid", "spx",
    )}
    values["address"] = parsed.hostname

    if scheme == "wireguard":
        public_key = _one(query, "publickey")
        if not public_key:
            raise ValueError("WireGuard configuration is missing the server public key")
        peer = {"endpoint": f"{parsed.hostname}:{parsed.port}", "publicKey": public_key}
        if _one(query, "presharedkey"):
            peer["preSharedKey"] = _one(query, "presharedkey")
        if _one(query, "keepalive"):
            peer["keepAlive"] = int(_one(query, "keepalive"))
        settings = {"secretKey": unquote(parsed.username), "peers": [peer], "noKernelTun": True}
        addresses = [item for item in _one(query, "address").split(",") if item]
        if addresses:
            settings["address"] = addresses
        if _one(query, "mtu"):
            settings["mtu"] = int(_one(query, "mtu"))
        return _base_config(local_port, {"protocol": "wireguard", "settings": settings})

    network = _one(query, "type", "tcp")
    security = _one(query, "security", "none")
    if scheme == "vless":
        user = {"id": unquote(parsed.username), "encryption": _one(query, "encryption", "none") or "none"}
        if _one(query, "flow"):
            user["flow"] = _one(query, "flow")
        settings = {"vnext": [{"address": parsed.hostname, "port": int(parsed.port), "users": [user]}]}
    else:
        settings = {"servers": [{"address": parsed.hostname, "port": int(parsed.port),
                                  "password": unquote(parsed.username)}]}
    return _base_config(local_port, {
        "protocol": scheme, "settings": settings,
        "streamSettings": _stream_settings(network, security, values),
    })


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
