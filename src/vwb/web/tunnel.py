"""Own one ngrok agent; never pool independent Workbench state or log traffic."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from vwb.integrations.callbacks import public_origin
from vwb.integrations.store import IntegrationError


class Ngrok:
    def __init__(self, port: int, origin: str = '', *, timeout: float = 25, executable: str | None = None):
        origin = public_origin(origin)
        binary = executable or shutil.which('ngrok')
        if not binary:
            raise ValueError('Install ngrok and configure its host authtoken, then run vwb ui --ngrok again. See docs/PUBLIC_ACCESS.md.')
        self.process = None
        self.directory = tempfile.TemporaryDirectory(prefix='vwb-ngrok-')
        self.log = Path(self.directory.name) / 'agent.jsonl'
        self.origin = ''
        command = [binary, 'http', f'http://127.0.0.1:{port}', '--inspect=false', '--log=stdout', '--log-format=json']
        if origin:
            command.extend(['--url', origin])
        try:
            with self.log.open('xb') as output:
                os.chmod(self.log, 0o600)
                # The supervisor owns ngrok and watches this pipe. EOF also
                # occurs if Workbench is killed without Python cleanup.
                self.process = subprocess.Popen([sys.executable, '-m', 'vwb.web.tunnel_guard', *command],
                                                stdin=subprocess.PIPE, stdout=output,
                                                stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            error_code = ''
            while time.monotonic() < deadline:
                if self.log.stat().st_size > 1_000_000:
                    raise ValueError('ngrok startup output exceeded its limit; no Workbench was exposed.')
                content = self.log.read_text(errors='replace')
                codes = re.findall(r'ERR_NGROK_[0-9]+', content)
                error_code = codes[-1] if codes else error_code
                for line in content.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and event.get('msg') == 'started tunnel' and isinstance(event.get('url'), str):
                        try:
                            selected = public_origin(event['url'])
                        except IntegrationError:
                            raise ValueError('ngrok returned an invalid public origin; the tunnel was stopped.') from None
                        if not selected or origin and selected != origin:
                            raise ValueError('ngrok returned a different public origin; the tunnel was stopped.')
                        self.origin = selected
                if self.process.poll() is not None:
                    raise ValueError(f'ngrok could not start{": " + error_code if error_code else ""}. Check its host authtoken, domain availability and account limits. No Workbench was exposed.')
                if self.origin:
                    return
                time.sleep(.05)
            raise ValueError('ngrok startup timed out. Check its host configuration and network connection; no Workbench was exposed.')
        except BaseException:
            self.close()
            raise

    def close(self):
        process = self.process
        if process and process.poll() is None:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=7)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        self.directory.cleanup()
