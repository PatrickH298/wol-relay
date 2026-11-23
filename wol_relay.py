#!/usr/bin/env python3
"""Tiny Wake-on-LAN relay.

Runs on a host that sits on the same LAN segment as the device(s) it wakes,
so it can send a real link-local broadcast magic packet instead of the
fragile unicast-to-a-specific-IP a remote network (like Home Assistant's
Docker bridge, across a VLAN) is stuck using. Broadcast doesn't depend on
an ARP entry for the target being fresh, which is what unicast WoL breaks
on after the target's been off for a while.

Each caller is a named "requester" with its own bearer token, and each
requester is either limited to a list of targets or explicitly allowed
all of them with "*". A requester's token can be given inline ("token"),
read from a file ("token_file", e.g. a Docker secret), or read from an
environment variable ("token_env").

No third-party dependencies — stdlib only, so there's nothing to pip
install on the host running this.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import socket
import urllib.request
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("wol-relay")

DEFAULT_CONFIG_PATH = "/etc/wol-relay/config.json"
SEND_COUNT = 3
SEND_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 10
MAX_BODY_BYTES = 64 * 1024
ALL_TARGETS = "*"
TOKEN_SOURCES = ("token", "token_file", "token_env")
LIMITED_BROADCAST = "255.255.255.255"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        config = json.load(f)

    if not isinstance(config, dict):
        raise ValueError("config: top level must be a JSON object")
    if "token" in config:
        raise ValueError(
            "config: top-level 'token' is no longer supported; move it into "
            "a 'requesters' entry with \"targets\": \"*\""
        )

    targets = config.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise ValueError("config: 'targets' must be a non-empty object")
    for name, target in targets.items():
        validate_target(name, target)

    requesters = config.get("requesters")
    if not isinstance(requesters, dict) or not requesters:
        raise ValueError("config: 'requesters' must be a non-empty object")
    seen_tokens: dict[str, str] = {}
    for name, requester in requesters.items():
        validate_requester(name, requester, targets)
        token = requester["token"]
        if token in seen_tokens:
            raise ValueError(
                f"config: requesters '{seen_tokens[token]}' and '{name}' "
                "share the same token; each token must be unique"
            )
        seen_tokens[token] = name

    config.setdefault("bind_host", "0.0.0.0")
    config.setdefault("bind_port", 8098)
    port = config["bind_port"]
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"config: invalid bind_port {port!r}")
    return config


def validate_target(name: str, target: object) -> None:
    if not isinstance(target, dict):
        raise ValueError(f"config: target '{name}' must be an object")
    if "mac" not in target:
        raise ValueError(f"config: target '{name}' is missing 'mac'")
    normalize_mac(target["mac"])  # raises if malformed

    port = target.get("port", 9)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ValueError(f"config: target '{name}' has invalid port {port!r}")

    for key in ("broadcast_address", "source_address"):
        if key in target:
            try:
                ipaddress.IPv4Address(target[key])
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"config: target '{name}' has invalid {key} {target[key]!r}"
                ) from exc

    # 255.255.255.255 with no source address leaves egress up to the default
    # route, which on a multi-VLAN host may not be the target's VLAN.
    if (
        target.get("broadcast_address", LIMITED_BROADCAST) == LIMITED_BROADCAST
        and "source_address" not in target
    ):
        log.warning(
            "target '%s' sends to %s with no source_address; the packet will "
            "leave via the default route's interface",
            name,
            LIMITED_BROADCAST,
        )


def validate_requester(name: str, requester: object, targets: dict) -> None:
    if not isinstance(requester, dict):
        raise ValueError(f"config: requester '{name}' must be an object")

    requester["token"] = resolve_token(name, requester)

    allowed = requester.get("targets")
    if allowed == ALL_TARGETS:
        return
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(
            f"config: requester '{name}' needs 'targets': either \"*\" for "
            "all targets or a non-empty list of target names"
        )
    for target_name in allowed:
        if target_name not in targets:
            raise ValueError(
                f"config: requester '{name}' lists unknown target "
                f"{target_name!r}"
            )


def resolve_token(name: str, requester: dict) -> str:
    """Get a requester's token from exactly one of its token sources.

    - "token":      the token itself, inline in the config
    - "token_file": path to a file holding the token (e.g. a Docker/Compose
                    secret under /run/secrets/); surrounding whitespace,
                    including a trailing newline, is stripped
    - "token_env":  name of an environment variable holding the token

    Error messages name the source but never include the token value.
    """
    sources = [key for key in TOKEN_SOURCES if key in requester]
    if len(sources) != 1:
        raise ValueError(
            f"config: requester '{name}' needs exactly one of "
            f"{', '.join(TOKEN_SOURCES)} (found {len(sources)})"
        )
    source = sources[0]
    ref = requester[source]
    if not isinstance(ref, str) or not ref:
        raise ValueError(
            f"config: requester '{name}' has an empty or non-string '{source}'"
        )

    if source == "token":
        token, where = ref, "inline token"
    elif source == "token_file":
        where = f"token_file {ref}"
        try:
            with open(ref, encoding="utf-8") as f:
                token = f.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"config: requester '{name}' can't read {where}: {exc}"
            ) from exc
    else:
        where = f"token_env ${ref}"
        token = os.environ.get(ref, "").strip()
        if not token:
            raise ValueError(
                f"config: requester '{name}' {where} is unset or empty"
            )

    if not token:
        raise ValueError(f"config: requester '{name}' {where} is empty")
    if not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError(
            f"config: requester '{name}' {where} must be printable ASCII "
            "with no spaces"
        )
    return token


def normalize_mac(mac: str) -> bytes:
    if not isinstance(mac, str):
        raise ValueError(f"{mac!r} is not a valid MAC address")
    cleaned = mac.strip().replace(":", "").replace("-", "").replace(".", "")
    if len(cleaned) != 12:
        raise ValueError(f"'{mac}' is not a valid MAC address")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"'{mac}' is not a valid MAC address") from exc


def build_magic_packet(mac: str) -> bytes:
    mac_bytes = normalize_mac(mac)
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet(
    mac: str,
    broadcast_address: str,
    port: int,
    source_address: str | None = None,
) -> None:
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # On a host with several VLAN interfaces on one NIC, the outgoing
        # interface for a broadcast send is otherwise picked by matching
        # the *destination* against the routing table — which is fine for
        # a subnet-directed broadcast (e.g. 10.30.10.255, since that
        # exactly matches one VLAN's connected route) but ambiguous for
        # 255.255.255.255, which has no subnet of its own and typically
        # goes out whatever the default route happens to be. Binding to
        # the target VLAN's own local IP first pins the egress interface
        # explicitly, regardless of which broadcast address is used, and
        # needs no elevated capabilities (unlike SO_BINDTODEVICE).
        if source_address:
            sock.bind((source_address, 0))
        for i in range(SEND_COUNT):
            sock.sendto(packet, (broadcast_address, port))
            if i < SEND_COUNT - 1:
                time.sleep(SEND_DELAY_SECONDS)


def make_handler(config: dict):
    targets = config["targets"]
    # (name, token bytes, allowed target names or None for all)
    requesters = [
        (
            name,
            r["token"].encode("utf-8"),
            None if r["targets"] == ALL_TARGETS else frozenset(r["targets"]),
        )
        for name, r in config["requesters"].items()
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "wol-relay/1.1"
        timeout = REQUEST_TIMEOUT_SECONDS

        def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
            log.info("%s - %s", self.client_address[0], fmt % args)

        def _send(self, status: HTTPStatus, body: dict) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _drain_body(self) -> None:
            # Unread request data left in the socket makes Linux send a RST
            # on close, which can make the client report a reset even
            # though it got a response.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            length = max(0, min(length, MAX_BODY_BYTES))
            if length:
                self.rfile.read(length)

        def _requester(self) -> tuple[str, frozenset | None] | None:
            """Return (name, allowed targets) for a valid token, else None."""
            header = self.headers.get("Authorization", "")
            scheme, _, supplied = header.partition(" ")
            if scheme.lower() != "bearer" or not supplied:
                return None
            supplied_bytes = supplied.strip().encode("utf-8")
            match = None
            # Check every token (no early exit) so timing doesn't reveal
            # which requester, if any, a guess is close to.
            for name, token, allowed in requesters:
                if hmac.compare_digest(supplied_bytes, token):
                    match = (name, allowed)
            return match

        def do_GET(self):  # noqa: N802 - stdlib signature
            path = urlparse(self.path).path
            if path == "/health":
                self._send(HTTPStatus.OK, {"status": "ok"})
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):  # noqa: N802 - stdlib signature
            self._drain_body()
            path = urlparse(self.path).path
            parts = path.strip("/").split("/")

            if len(parts) != 2 or parts[0] != "wake":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            requester = self._requester()
            if requester is None:
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            requester_name, allowed = requester

            name = parts[1]
            target = targets.get(name)
            # A restricted requester gets the same 404 for a target that
            # exists but isn't theirs as for one that doesn't exist, so it
            # can't discover other target names. The log has the real reason.
            if target is None or (allowed is not None and name not in allowed):
                if target is not None:
                    log.warning(
                        "requester '%s' denied wake for target '%s' (not allowed)",
                        requester_name,
                        name,
                    )
                self._send(
                    HTTPStatus.NOT_FOUND, {"error": f"unknown target '{name}'"}
                )
                return

            broadcast_address = target.get("broadcast_address", LIMITED_BROADCAST)
            port = target.get("port", 9)
            source_address = target.get("source_address")
            try:
                send_magic_packet(
                    target["mac"], broadcast_address, port, source_address
                )
            except OSError as exc:
                log.exception(
                    "send failed for target '%s' (requester '%s')",
                    name,
                    requester_name,
                )
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return

            log.info(
                "requester '%s' woke '%s' (%s) via %s:%d%s",
                requester_name,
                name,
                target["mac"],
                broadcast_address,
                port,
                f" from {source_address}" if source_address else "",
            )
            self._send(HTTPStatus.OK, {"status": "sent", "target": name})

    return Handler


def healthcheck(config_path: str) -> int:
    try:
        with open(config_path, encoding="utf-8") as f:
            raw = json.load(f)
        host = raw.get("bind_host", "0.0.0.0")
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        port = raw.get("bind_port", 8098)
        # No proxies: an inherited HTTP_PROXY must not intercept a local check.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/health", timeout=3) as r:
            return 0 if r.status == 200 else 1
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"wol-relay healthcheck failed: {exc}")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="GET /health on the configured bind address and exit 0 if OK "
        "(for container HEALTHCHECK); doesn't resolve tokens",
    )
    args = parser.parse_args()

    if args.healthcheck:
        raise SystemExit(healthcheck(args.config))

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"wol-relay: {exc}") from None

    handler = make_handler(config)
    server = ThreadingHTTPServer((config["bind_host"], config["bind_port"]), handler)
    log.info(
        "wol-relay listening on %s:%d, targets: %s, requesters: %s",
        config["bind_host"],
        config["bind_port"],
        ", ".join(config["targets"]),
        ", ".join(config["requesters"]),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
