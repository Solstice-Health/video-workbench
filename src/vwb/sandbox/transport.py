"""Multiplex public proxy connections and child output over Docker attached stdio."""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import threading
from types import SimpleNamespace

from vwb.sandbox.egress import Handler

MAX_MODEL_REQUEST = 32 * 1024 * 1024
MAX_MODEL_RESPONSE = 256 * 1024
MAX_FRAME = 4 * ((MAX_MODEL_REQUEST + 2) // 3) + 1024


class PublicProcess:
    """Popen-like wrapper; stdout/stderr expose only the agent command's output."""
    def __init__(self, process, *, model_handler=None, allow_public=True):
        self.process = process
        out_read, self.out_write = os.pipe()
        err_read, self.err_write = os.pipe()
        self.stdout, self.stderr = os.fdopen(out_read, 'rb'), os.fdopen(err_read, 'rb')
        self.lock = threading.Lock()
        self.connections = {}
        self.allow_public = allow_public
        self.model_handler = model_handler
        self.model_slots = threading.BoundedSemaphore(4)
        self.model_channels = set()
        self.model_lock = threading.Lock()
        self.stop = threading.Event()
        self.errors_done = threading.Event()
        self.server = SimpleNamespace(slots=threading.BoundedSemaphore(16), stop=self.stop)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.errors = threading.Thread(target=self._errors, daemon=True)
        self.errors.start()

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        result = self.process.wait(timeout=timeout)
        self.reader.join(timeout=2)
        self.errors.join(timeout=2)
        return result

    def kill(self):
        self.stop.set()
        self.process.kill()

    def _emit(self, kind, channel, data=b''):
        packet = json.dumps({'kind': kind, 'channel': channel,
                             'data': base64.b64encode(data).decode('ascii')}).encode() + b'\n'
        try:
            with self.lock:
                self.process.stdin.write(packet)
                self.process.stdin.flush()
        except (OSError, ValueError):
            self.stop.set()

    def _serve(self, channel, upstream, proxy):
        def serve():
            try:
                Handler(proxy, None, self.server)
            finally:
                proxy.close()
        threading.Thread(target=serve, daemon=True).start()
        try:
            while not self.stop.is_set():
                data = upstream.recv(32768)
                if not data:
                    break
                self._emit('data', channel, data)
        except OSError:
            pass
        finally:
            self.connections.pop(channel, None)
            upstream.close()
            self._emit('close', channel)

    def _model(self, channel, data):
        try:
            row = json.loads(data)
            if (not isinstance(row, dict) or set(row) != {'tool', 'arguments'}
                    or row['tool'] not in ('model_providers', 'model_catalog', 'model_request')
                    or not isinstance(row['arguments'], dict)):
                raise ValueError('Invalid model bridge request')
            if self.stop.is_set():
                raise ValueError('Sandbox model bridge stopped')
            result = self.model_handler(row['tool'], row['arguments'], self.stop)
            response = json.dumps(result, allow_nan=False, ensure_ascii=False).encode('utf-8')
            if len(response) > MAX_MODEL_RESPONSE:
                response = json.dumps({'is_error': bool(result.get('is_error')), 'text': str(result.get('text', ''))[:60_000],
                                       'data': None, 'note': 'Read the saved response path in the tool receipt.'},
                                      ensure_ascii=False).encode('utf-8')
        except Exception:
            # Tool errors themselves are serialized by Registry.call. A bridge
            # failure must not reveal host configuration or an exception repr.
            response = b'{"is_error":true,"text":"Model bridge request failed or was interrupted."}'
        finally:
            with self.model_lock:
                self.model_channels.discard(channel)
            self.model_slots.release()
        if not self.stop.is_set():
            self._emit('model_response', channel, response)

    def _model_request(self, channel, data):
        with self.model_lock:
            if (self.model_handler is None or not 0 < len(data) <= MAX_MODEL_REQUEST
                    or channel in self.model_channels or not self.model_slots.acquire(blocking=False)):
                self._emit('model_response', channel,
                           b'{"is_error":true,"text":"Model access is disabled, busy or invalid."}')
                return
            self.model_channels.add(channel)
        threading.Thread(target=self._model, args=(channel, data), daemon=True).start()

    def _read(self):
        try:
            while line := self.process.stdout.readline(MAX_FRAME):
                try:
                    if not line.endswith(b'\n'):
                        raise ValueError('Invalid network transport frame')
                    row = json.loads(line)
                    channel, kind = row['channel'], row['kind']
                    if type(channel) is not int or channel < 0:
                        raise ValueError('Invalid transport channel')
                    data = base64.b64decode(row.get('data', ''), validate=True)
                    if kind in ('stdout', 'stderr'):
                        os.write(self.out_write if kind == 'stdout' else self.err_write, data)
                    elif kind == 'model_request':
                        self._model_request(channel, data)
                    elif kind == 'open':
                        if not self.allow_public or channel in self.connections or len(self.connections) >= 16:
                            self._emit('close', channel)
                            continue
                        upstream, proxy = socket.socketpair()
                        upstream.settimeout(15)
                        self.connections[channel] = upstream
                        threading.Thread(target=self._serve, args=(channel, upstream, proxy), daemon=True).start()
                    elif channel in self.connections:
                        if kind == 'data':
                            self.connections[channel].sendall(data)
                        else:
                            self.connections[channel].shutdown(socket.SHUT_WR)
                except (ValueError, KeyError, TypeError, OSError):
                    continue
        finally:
            self.stop.set()
            for connection in list(self.connections.values()):
                connection.close()
            os.close(self.out_write)
            # stderr closes after the Docker client's separate diagnostics drain.
            self.errors_done.wait(2)
            os.close(self.err_write)

    def _errors(self):
        try:
            while data := self.process.stderr.read(8192):
                os.write(self.err_write, data)
        except (OSError, ValueError, io.UnsupportedOperation):
            pass
        finally:
            self.errors_done.set()
