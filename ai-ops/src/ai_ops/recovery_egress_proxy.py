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

import argparse
import ipaddress
import re
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
# WF10 CI relay(2026-11-26): 정상 CI 영수증 artifact의 서명 다운로드 위치. GitHub 소유 저장소만 정확한 패턴으로
# 허용한다(실측 302 Location: productionresultssa<N>.blob.core.windows.net). ``--github-artifacts``일 때만 켠다.
GITHUB_ARTIFACTS = (re.compile(r'productionresultssa(?:0|[1-9][0-9]?)\.blob\.core\.windows\.net:443'),
                    re.compile(r'results-receiver\.actions\.githubusercontent\.com:443'))


def parse_connect(raw: bytes, targets=TARGETS, patterns=()) -> str:
    """Return the allowed ``host:443`` or raise; one CONNECT line and one Host header only."""
    if len(raw) > MAX_HEADER or not raw.endswith(b'\r\n\r\n'):
        raise ValueError('proxy_header_rejected')
    try:
        lines = raw[:-4].decode('ascii').split('\r\n')
    except UnicodeDecodeError:
        raise ValueError('proxy_header_rejected') from None
    parts = lines[0].split(' ')
    allowed = parts[1] in targets or any(p.fullmatch(parts[1]) for p in patterns) if len(parts) == 3 else False
    if (len(parts) != 3 or parts[0] != 'CONNECT' or not allowed
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
            target = parse_connect(bytes(raw), server.targets, server.patterns)
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

    def __init__(self, address, *, targets=TARGETS, patterns=(), resolver=socket.getaddrinfo, connect=_connect,
                 clock=time.monotonic, max_tunnels=MAX_TUNNELS):
        if not targets or any(not t.endswith(':443') for t in targets):
            raise ValueError('proxy_targets_rejected')
        if any(not p.pattern.endswith(':443') for p in patterns):
            raise ValueError('proxy_targets_rejected')
        self.targets, self.resolver, self.connect, self.clock = tuple(targets), resolver, connect, clock
        self.patterns = tuple(patterns)
        self.slots = threading.BoundedSemaphore(max_tunnels)
        super().__init__(address, _Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fixed-destination CONNECT proxy (no logging)')
    parser.add_argument('--github-artifacts', action='store_true',
                        help='also relay GitHub Actions artifact storage (WF10 CI relay)')
    args = parser.parse_args(argv)
    patterns = GITHUB_ARTIFACTS if args.github_artifacts else ()
    with ProxyServer(('0.0.0.0', PORT), patterns=patterns) as server:
        server.serve_forever(poll_interval=1)


if __name__ == '__main__':
    main()
