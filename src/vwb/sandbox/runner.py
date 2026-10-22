"""Trusted container relay. Public HTTP proxy travels over framed Docker stdio.

The child only sees a loopback HTTP proxy; the container has no network interface.
The host validates destinations. This transport works on Docker Desktop too.
"""
import base64
import http.server
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
_model_requests = {}
_model_slots = threading.BoundedSemaphore(4)
MAX_MODEL_REQUEST = 32 * 1024 * 1024
MAX_MODEL_RESPONSE = 256 * 1024


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
                model = _model_requests.get(row['channel'])
            if row['kind'] == 'model_response' and model:
                data = base64.b64decode(row['data'], validate=True)
                if len(data) <= MAX_MODEL_RESPONSE:
                    model['data'] = data
                    model['ready'].set()
                continue
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


class ModelRelay(http.server.BaseHTTPRequestHandler):
    """Only the three scoped model tools can cross this dedicated channel."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        global _serial
        lengths = self.headers.get_all('Content-Length', [])
        try:
            if (self.path != '/request' or self.headers.get('Transfer-Encoding') or len(lengths) != 1
                    or self.headers.get_content_type() != 'application/json'):
                raise ValueError('Invalid model request')
            size = int(lengths[0])
            if not 0 < size <= MAX_MODEL_REQUEST:
                raise ValueError('Model request exceeds 32 MiB')
            self.connection.settimeout(30)
            body = self.rfile.read(size)
            row = json.loads(body)
            if (len(body) != size or not isinstance(row, dict)
                    or set(row) != {'tool', 'arguments'}
                    or row['tool'] not in ('model_providers', 'model_catalog', 'model_request')
                    or not isinstance(row['arguments'], dict)):
                raise ValueError('Invalid model request')
        except (ValueError, OSError):
            self.send_error(400, 'Invalid model request')
            return
        if not _model_slots.acquire(blocking=False):
            self.send_error(503, 'Four model requests are already active')
            return
        model = {'ready': threading.Event(), 'data': None}
        with _connections_lock:
            _serial += 1
            channel = _serial
            _model_requests[channel] = model
        try:
            emit('model_request', channel, body)
            if not model['ready'].wait(1800):
                self.send_error(504, 'Model request timed out')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(model['data'])))
            self.end_headers()
            self.wfile.write(model['data'])
        except OSError:
            pass
        finally:
            with _connections_lock:
                _model_requests.pop(channel, None)
            _model_slots.release()


def capture(stream, kind):
    while data := stream.read1(8192):
        emit(kind, data=data)


if __name__ == '__main__':
    split = sys.argv.index('--')
    options, argv = sys.argv[1:split], sys.argv[split + 1:]
    servers = []
    if '--public' in options:
        proxy = Server(('127.0.0.1', 0), Relay)
        servers.append(proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        address = f'http://127.0.0.1:{proxy.server_address[1]}'
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = address
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = ''
    if '--models' in options:
        models = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ModelRelay)
        models.daemon_threads = True
        servers.append(models)
        threading.Thread(target=models.serve_forever, daemon=True).start()
        os.environ['VWB_MODEL_ENDPOINT'] = f'http://127.0.0.1:{models.server_address[1]}/request'
    threading.Thread(target=receive, daemon=True).start()
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    readers = [threading.Thread(target=capture, args=(child.stdout, 'stdout'), daemon=True),
               threading.Thread(target=capture, args=(child.stderr, 'stderr'), daemon=True)]
    for reader in readers:
        reader.start()
    code = child.wait()
    for reader in readers:
        reader.join()
    for server in servers:
        server.shutdown()
    # The stdin receiver deliberately remains available while the child is
    # alive. Bypass buffered-stdio finalization with that daemon still waiting.
    sys.stdout.flush()
    os._exit(code if code >= 0 else 128 - code)
