"""Public HTTP(S) proxy reached through attached stdio, without a Docker network.

DNS is checked for every connection and the socket connects to that exact numeric
address, so DNS rebinding cannot redirect a validated hostname to a private host.
No request is made through ambient host proxy settings or provider credentials.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

MAX_HEADERS = 32_768
MAX_CONNECTION_BYTES = 256 * 1024 * 1024
SPECIAL = tuple(ipaddress.ip_network(value) for value in (
    '64:ff9b::/96', '64:ff9b:1::/48',  # NAT64 can encode private IPv4 destinations
    '168.63.129.16/32',              # Azure platform/metadata virtual address
))


def public_addresses(host: str, port: int) -> list[tuple]:
    if port not in (80, 443) or not host or len(host) > 253 or '%' in host:
        raise ValueError('Only public HTTP on port 80 and HTTPS on port 443 are available')
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not rows:
        raise ValueError('No public address was found')
    for row in rows:
        address = ipaddress.ip_address(row[4][0])
        if (not address.is_global or address.is_multicast or address.is_unspecified
                or any(address in network for network in SPECIAL)
                or getattr(address, 'ipv4_mapped', None) is not None
                or getattr(address, 'sixtofour', None) is not None
                or getattr(address, 'teredo', None) is not None):
            raise ValueError('Private, local and special-purpose destinations are unavailable')
    return rows


def connect_public(host: str, port: int) -> socket.socket:
    for family, kind, proto, _, address in public_addresses(host, port):
        connection = socket.socket(family, kind, proto)
        connection.settimeout(15)
        try:
            connection.connect(address)  # pinned address, no second hostname lookup
            return connection
        except OSError:
            connection.close()
    raise OSError('The public destination did not accept a connection')


def relay(left: socket.socket, right: socket.socket, stop: threading.Event | None = None) -> None:
    total, idle = 0, time.monotonic()
    while not (stop and stop.is_set()) and time.monotonic() - idle < 60:
        ready, _, _ = select.select([left, right], [], [], 1)
        for source in ready:
            data = source.recv(65_536)
            if not data:
                return
            total += len(data)
            if total > MAX_CONNECTION_BYTES:
                return
            (right if source is left else left).sendall(data)
            idle = time.monotonic()


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            self.request.sendall(b'HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n')
            return
        upstream = None
        try:
            self.request.settimeout(15)
            data = bytearray()
            while b'\r\n\r\n' not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
                if len(data) > MAX_HEADERS:
                    raise ValueError('Proxy headers are too large')
            raw, initial = bytes(data).split(b'\r\n\r\n', 1)
            lines = raw.decode('latin-1').split('\r\n')
            method, target, version = lines[0].split(' ')
            if version not in ('HTTP/1.0', 'HTTP/1.1'):
                raise ValueError('Unsupported proxy protocol')
            headers = []
            for line in lines[1:]:
                if ':' not in line or line[:1].isspace():
                    raise ValueError('Malformed proxy headers')
                name, value = line.split(':', 1)
                if any(ord(c) < 33 or ord(c) > 126 for c in name):
                    raise ValueError('Malformed proxy header name')
                headers.append((name.lower(), value.strip()))
            if method == 'CONNECT':
                parsed = urlsplit('https://' + target)
                if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                    raise ValueError('Invalid CONNECT target')
                port = parsed.port or 443
                if port != 443:
                    raise ValueError('CONNECT requires port 443')
                upstream = connect_public(parsed.hostname or '', port)
                self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                if initial:
                    upstream.sendall(initial)
                relay(self.request, upstream, self.server.stop)
            else:
                # One HTTP request per socket. No pipelining, upgrades or chunked
                # request bodies; HTTPS tunnels support ordinary client protocols.
                parsed = urlsplit(target)
                if (parsed.scheme != 'http' or parsed.username or parsed.password or parsed.fragment
                        or method not in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')):
                    raise ValueError('Use an absolute public HTTP URL or HTTPS CONNECT')
                port = parsed.port or 80
                if port != 80:
                    raise ValueError('HTTP requires port 80')
                lengths = [v for k, v in headers if k == 'content-length']
                if len(lengths) > 1 or any(k in ('transfer-encoding', 'upgrade') for k, _ in headers):
                    raise ValueError('Chunked bodies, upgrades and duplicate lengths require HTTPS')
                length = int(lengths[0]) if lengths else 0
                if length < 0 or length > 16 * 1024 * 1024:
                    raise ValueError('HTTP body is too large')
                upstream = connect_public(parsed.hostname or '', port)
                host = parsed.hostname or ''
                authority = f'[{host}]' if ':' in host else host
                path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
                hop = {'host', 'connection', 'proxy-connection', 'proxy-authorization',
                       'proxy-authenticate', 'keep-alive', 'te', 'trailer'}
                hop.update(part.strip().lower() for key, value in headers if key == 'connection'
                           for part in value.split(','))
                forwarded = '\r\n'.join(f'{k}: {v}' for k, v in headers if k not in hop)
                head = f'{method} {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n'
                if forwarded:
                    head += forwarded + '\r\n'
                upstream.sendall((head + '\r\n').encode('latin-1'))
                sent = min(length, len(initial))
                upstream.sendall(initial[:sent])
                while sent < length:
                    chunk = self.request.recv(min(65_536, length - sent))
                    if not chunk:
                        raise ValueError('Incomplete HTTP body')
                    upstream.sendall(chunk)
                    sent += len(chunk)
                total = 0
                while not self.server.stop.is_set():
                    chunk = upstream.recv(65_536)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > MAX_CONNECTION_BYTES:
                        return
                    self.request.sendall(chunk)
        except (OSError, ValueError):
            try:
                self.request.sendall(b'HTTP/1.1 403 Destination unavailable\r\nConnection: close\r\n\r\n'
                                     b'Only public HTTP(S) destinations are available.\n')
            except OSError:
                pass
        finally:
            if upstream:
                upstream.close()
            self.server.slots.release()
