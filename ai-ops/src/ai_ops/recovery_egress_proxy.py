"""Fixed-destination CONNECT proxy for the WF07 relay reads (user decision 2026-11-24).

WF10 design B' keeps the main Pod (receiver/worker/relay) without internet egress;
only separate Pods talk to the internet. WF07 preparation needs three TLS
destinations, so a small proxy Pod relays exactly those:

* ``api.github.com:443`` (isolated-repository Git data, read-only token);
* ``sts.ap-northeast-2.amazonaws.com:443`` (IRSA AssumeRoleWithWebIdentity,
  GetCallerIdentity);
* ``api.ecr.ap-northeast-2.amazonaws.com:443`` (DescribeImages).

TLS stays end to end between the relay and the destination: this process never
sees tokens, credentials or bodies and holds no secret. One ``CONNECT host:443``
line plus one matching ``Host`` header is accepted; every DNS answer must be a
public IPv4 address (no private/link-local/metadata targets, no DNS rebinding to
internal ranges). Tunnels are bounded (concurrency, idle and total lifetime).
Nothing is logged: requests, TLS bytes and errors are never written.
Separate from ``model_egress_proxy`` (practice OpenAI proxy), which is unchanged.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
import time

TARGETS = ('api.github.com:443', 'sts.ap-northeast-2.amazonaws.com:443',
           'api.ecr.ap-northeast-2.amazonaws.com:443')
MAX_HEADER = 4096
MAX_TUNNELS = 8
IDLE_SECONDS = 35
LIFETIME_SECONDS = 120
PORT = 8080


def parse_connect(raw: bytes, targets=TARGETS) -> str:
    """Return the allowed ``host:443`` or raise; one CONNECT line and one Host header only."""
    if len(raw) > MAX_HEADER or not raw.endswith(b'\r\n\r\n'):
        raise ValueError('proxy_header_rejected')
    try:
        lines = raw[:-4].decode('ascii').split('\r\n')
    except UnicodeDecodeError:
        raise ValueError('proxy_header_rejected') from None
    parts = lines[0].split(' ')
    if (len(parts) != 3 or parts[0] != 'CONNECT' or parts[1] not in targets
            or parts[2] not in ('HTTP/1.0', 'HTTP/1.1') or len(lines) != 2):
        raise ValueError('proxy_target_rejected')
    key, sep, value = lines[1].partition(':')
    if not sep or key.lower() != 'host' or value.strip().lower() != parts[1]:
        raise ValueError('proxy_header_rejected')
    return parts[1]


def public_addresses(target, resolver=socket.getaddrinfo) -> list[tuple]:
    """Every answer must be a public IPv4 address; one internal answer rejects all."""
    host, _, port = target.rpartition(':')
    answers = resolver(host, int(port), family=socket.AF_INET, type=socket.SOCK_STREAM)
    if not answers:
        raise ValueError('proxy_dns_empty')
    for family, _kind, _proto, _canon, sockaddr in answers:
        if family != socket.AF_INET or not ipaddress.ip_address(sockaddr[0]).is_global:
            raise ValueError('proxy_dns_rejected')
    return answers


def _connect(sockaddr):
    return socket.create_connection(sockaddr, timeout=5)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server, client = self.server, self.request
        if not server.slots.acquire(blocking=False):
            try:
                client.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            return
        try:
            self._tunnel(server, client)
        finally:
            server.slots.release()

    def _tunnel(self, server, client):
        client.settimeout(5)
        raw = bytearray()
        try:
            while not raw.endswith(b'\r\n\r\n'):
                if len(raw) > MAX_HEADER:
                    raise ValueError('proxy_header_rejected')
                part = client.recv(1)
                if not part:
                    return
                raw.extend(part)
            target = parse_connect(bytes(raw), server.targets)
            address = public_addresses(target, server.resolver)[0][4]
            upstream = server.connect(address)
        except (OSError, ValueError):
            try:  # fixed reply; request/target/error text is never echoed or logged
                client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            return
        with upstream:
            try:
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                deadline = server.clock() + LIFETIME_SECONDS
                sockets = (client, upstream)
                while True:
                    if server.clock() >= deadline:  # total lifetime, even with steady traffic
                        return
                    readable, _, _ = select.select(sockets, [], [], IDLE_SECONDS)
                    if not readable:  # idle
                        return
                    for current in readable:
                        data = current.recv(16384)
                        if not data:
                            return
                        (upstream if current is client else client).sendall(data)
            except OSError:
                return


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, *, targets=TARGETS, resolver=socket.getaddrinfo, connect=_connect,
                 clock=time.monotonic, max_tunnels=MAX_TUNNELS):
        if not targets or any(not t.endswith(':443') for t in targets):
            raise ValueError('proxy_targets_rejected')
        self.targets, self.resolver, self.connect, self.clock = tuple(targets), resolver, connect, clock
        self.slots = threading.BoundedSemaphore(max_tunnels)
        super().__init__(address, _Handler)


def main():
    with ProxyServer(('0.0.0.0', PORT)) as server:
        server.serve_forever(poll_interval=1)


if __name__ == '__main__':
    main()
