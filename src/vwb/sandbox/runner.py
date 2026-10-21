"""Trusted container relay. Public HTTP proxy travels over framed Docker stdio.

The child only sees a loopback HTTP proxy; the container has no network interface.
The host validates destinations. This transport works on Docker Desktop too.
"""
import base64
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading

_write_lock = threading.Lock()
_connections = {}
_connections_lock = threading.Lock()
_serial = 0


def emit(kind, channel=0, data=b''):
    row = json.dumps({'kind': kind, 'channel': channel, 'data': base64.b64encode(data).decode('ascii')})
    with _write_lock:
        sys.stdout.write(row + '\n')
        sys.stdout.flush()


def receive():
    for line in sys.stdin.buffer:
        try:
            row = json.loads(line)
            with _connections_lock:
                connection = _connections.get(row['channel'])
            if connection:
                if row['kind'] == 'data':
                    connection.sendall(base64.b64decode(row['data'], validate=True))
                else:
                    connection.shutdown(socket.SHUT_RDWR)
        except (ValueError, KeyError, OSError):
            continue


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        global _serial
        with _connections_lock:
            if len(_connections) >= 16:
                self.request.sendall(b'HTTP/1.1 503 Busy\r\n\r\n')
                return
            _serial += 1
            channel = _serial
            _connections[channel] = self.request
        self.request.settimeout(60)
        try:
            emit('open', channel)
            while data := self.request.recv(32768):
                emit('data', channel, data)
        except OSError:
            pass
        finally:
            with _connections_lock:
                _connections.pop(channel, None)
            emit('close', channel)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


def capture(stream, kind):
    while data := stream.read1(8192):
        emit(kind, data=data)


if __name__ == '__main__':
    threading.Thread(target=receive, daemon=True).start()
    with Server(('127.0.0.1', 0), Relay) as proxy:
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        address = f'http://127.0.0.1:{proxy.server_address[1]}'
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = address
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = ''
        child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        readers = [threading.Thread(target=capture, args=(child.stdout, 'stdout'), daemon=True),
                   threading.Thread(target=capture, args=(child.stderr, 'stderr'), daemon=True)]
        for reader in readers:
            reader.start()
        code = child.wait()
        for reader in readers:
            reader.join()
        proxy.shutdown()
        # The stdin receiver deliberately remains available while the child is
        # alive. Bypass buffered-stdio finalization with that daemon still waiting.
        sys.stdout.flush()
        os._exit(code if code >= 0 else 128 - code)
