"""Low-level, secret-safe connectivity diagnostics for Telegram routes."""

from __future__ import annotations

import base64
import re
import socket
import ssl
import time
from typing import Callable

TELEGRAM_HOST = "api.telegram.org"
TELEGRAM_PORT = 443


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def redact_connection_error(error: object, secrets=()) -> str:
    """Return a bounded error string without credentials or bot tokens."""
    message = str(error or "Unknown connection error")
    for secret in secrets:
        if secret:
            message = message.replace(str(secret), "***")
    message = re.sub(
        r"(?i)(https?|socks5h?)://([^\s/@:]+):([^\s/@]+)@",
        r"\1://***:***@",
        message,
    )
    message = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot***", message)
    return message[:500]


def classify_telegram_connection_error(error: object, secrets=()) -> tuple[str, str]:
    """Return a stable code and an operator-friendly, secret-safe message."""
    safe = redact_connection_error(error, secrets)
    lowered = safe.lower()
    if any(marker in lowered for marker in (
        "sslzeroreturnerror", "tls/ssl connection has been closed", "closed (eof)",
        "unexpected_eof_while_reading", "unexpected eof", "eof occurred",
    )):
        return (
            "route_outbound_closed",
            "The selected route opened its local proxy, but closed Telegram's TLS connection. "
            "The Xray configuration may be expired, disabled, incompatible, or unable to reach Telegram.",
        )
    if "timed out" in lowered or "timeout" in lowered:
        return "telegram_api_timeout", "Telegram did not respond through this route before the timeout."
    if "connection refused" in lowered:
        return "route_refused", "The selected proxy route refused the connection."
    return "telegram_api_failed", safe


def _stage(name: str, status: str, started: float, *, code=None, message=None) -> dict:
    result = {"name": name, "status": status, "latency_ms": _elapsed_ms(started)}
    if code:
        result["error_code"] = code
    if message:
        result["message"] = message
    return result


def _failure_code(exc: Exception, prefix: str) -> str:
    text = str(exc).lower()
    if "auth" in text or "407" in text or "0x02" in text:
        return f"{prefix}_auth_failed"
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in text:
        return f"{prefix}_timeout"
    if "refused" in text:
        return f"{prefix}_refused"
    if isinstance(exc, ssl.SSLError):
        return classify_telegram_connection_error(exc)[0]
    return f"{prefix}_failed"


def _http_connect_socket(host, port, username, password, timeout, target_host, target_port):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    headers = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: keep-alive",
    ]
    if username or password:
        raw = f"{username or ''}:{password or ''}".encode("utf-8")
        headers.append("Proxy-Authorization: Basic " + base64.b64encode(raw).decode("ascii"))
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response and len(response) < 16384:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    status_line = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if status != 200:
        sock.close()
        if status == 407:
            raise PermissionError("HTTP proxy authentication failed (407)")
        raise ConnectionError(f"HTTP proxy CONNECT failed ({status or 'invalid response'})")
    return sock


def _recv_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Proxy closed the connection unexpectedly")
        data += chunk
    return data


def _socks5_connect_socket(host, port, username, password, timeout, target_host, target_port):
    """Minimal RFC 1928/1929 CONNECT client with remote DNS resolution."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    methods = b"\x00\x02" if username or password else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 0xFF:
        sock.close()
        raise ConnectionError("SOCKS5 proxy rejected authentication methods")
    if method == 2:
        user = (username or "").encode("utf-8")
        secret = (password or "").encode("utf-8")
        if len(user) > 255 or len(secret) > 255:
            sock.close()
            raise ValueError("SOCKS5 credentials are too long")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        auth_version, auth_status = _recv_exact(sock, 2)
        if auth_version != 1 or auth_status != 0:
            sock.close()
            raise PermissionError("SOCKS5 authentication failed")
    elif method != 0:
        sock.close()
        raise ConnectionError("SOCKS5 proxy selected an unsupported authentication method")

    domain = target_host.encode("idna")
    if len(domain) > 255:
        sock.close()
        raise ValueError("Target hostname is too long")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + int(target_port).to_bytes(2, "big"))
    version, reply, _reserved, address_type = _recv_exact(sock, 4)
    if version != 5 or reply != 0:
        sock.close()
        descriptions = {
            1: "general failure", 2: "connection not allowed", 3: "network unreachable",
            4: "host unreachable", 5: "connection refused", 6: "TTL expired",
            7: "command unsupported", 8: "address type unsupported",
        }
        raise ConnectionError(f"SOCKS5 tunnel failed: {descriptions.get(reply, 'unknown error')}")
    if address_type == 1:
        _recv_exact(sock, 4)
    elif address_type == 3:
        _recv_exact(sock, _recv_exact(sock, 1)[0])
    elif address_type == 4:
        _recv_exact(sock, 16)
    else:
        sock.close()
        raise ConnectionError("SOCKS5 proxy returned an invalid address type")
    _recv_exact(sock, 2)
    return sock


def probe_telegram_transport(
    *, proxy_type=None, host=None, port=None, username=None, password=None,
    timeout=5.0, socket_factory: Callable = socket.create_connection,
) -> dict:
    """Probe endpoint, tunnel, and TLS separately; never include credentials."""
    stages = []
    secrets = (username, password)
    tunnel = None

    if proxy_type:
        started = time.perf_counter()
        try:
            check = socket_factory((host, int(port)), timeout=timeout)
            check.close()
            stages.append(_stage("proxy_tcp", "passed", started))
        except Exception as exc:
            message = redact_connection_error(exc, secrets)
            stages.append(_stage(
                "proxy_tcp", "failed", started,
                code=_failure_code(exc, "proxy_tcp"), message=message,
            ))
            return {"success": False, "stages": stages, "error": message,
                    "error_code": stages[-1]["error_code"]}

        started = time.perf_counter()
        try:
            if proxy_type == "socks5":
                tunnel = _socks5_connect_socket(
                    host, int(port), username, password, timeout,
                    TELEGRAM_HOST, TELEGRAM_PORT,
                )
            elif proxy_type == "http":
                tunnel = _http_connect_socket(
                    host, int(port), username, password, timeout,
                    TELEGRAM_HOST, TELEGRAM_PORT,
                )
            else:
                raise ValueError("Unsupported proxy type")
            stages.append(_stage("proxy_tunnel", "passed", started))
        except Exception as exc:
            if tunnel:
                tunnel.close()
            message = redact_connection_error(exc, secrets)
            stages.append(_stage(
                "proxy_tunnel", "failed", started,
                code=_failure_code(exc, "proxy_tunnel"), message=message,
            ))
            return {"success": False, "stages": stages, "error": message,
                    "error_code": stages[-1]["error_code"]}
    else:
        started = time.perf_counter()
        try:
            tunnel = socket_factory((TELEGRAM_HOST, TELEGRAM_PORT), timeout=timeout)
            tunnel.settimeout(timeout)
            stages.append(_stage("telegram_tcp", "passed", started))
        except Exception as exc:
            message = redact_connection_error(exc)
            stages.append(_stage(
                "telegram_tcp", "failed", started,
                code=_failure_code(exc, "telegram_tcp"), message=message,
            ))
            return {"success": False, "stages": stages, "error": message,
                    "error_code": stages[-1]["error_code"]}

    started = time.perf_counter()
    tls_socket = None
    try:
        context = ssl.create_default_context()
        tls_socket = context.wrap_socket(tunnel, server_hostname=TELEGRAM_HOST)
        stages.append(_stage("telegram_tls", "passed", started))
    except Exception as exc:
        code, message = classify_telegram_connection_error(exc, secrets)
        stages.append(_stage(
            "telegram_tls", "failed", started,
            code=code, message=message,
        ))
        return {"success": False, "stages": stages, "error": message,
                "error_code": stages[-1]["error_code"]}
    finally:
        if tls_socket:
            tls_socket.close()
        elif tunnel:
            tunnel.close()

    return {"success": True, "stages": stages}
