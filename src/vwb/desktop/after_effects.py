"""Native After Effects automation, deliberately separate from the Docker sandbox.

Only the human-facing host API can enable this bridge. JSX runs with the desktop
user's permissions, not in a security sandbox. Dispatch is durable and never replayed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import platform
import plistlib
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from filelock import FileLock

from vwb import config
from vwb.workspace import Workspace

TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'interrupted'}
JOB_ID = re.compile(r'[a-f0-9]{32}\Z')
MAX_SCRIPT_BYTES = 512 * 1024
MAX_RECEIPT_BYTES = 256 * 1024
LOG_BYTES = 8 * 1024 * 1024
TRUST_NOTICE = ('After Effects scripts run as your desktop user and can read or change local files and use '
                'the network. Workspace output paths are a convention, not a security sandbox. '
                'Enable only for trusted conversations. Cancelling a script stops waiting; it may still run in After Effects.')
SETUP_STEPS = [
    'Install and sign in to Adobe After Effects on this Mac.',
    'In After Effects Settings > Scripting & Expressions, enable Allow Scripts To Write Files And Access Network.',
    'Enable local automation here. On the first script, allow macOS Automation access to After Effects when prompted.',
    'Save your current project before creating a separate Workbench project. Scripts never silently discard unsaved changes.',
]


def _private_root(runs_dir=None, *, create=True):
    root = Path(runs_dir if runs_dir is not None else config.RUNS_DIR).resolve()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    for part in (root / '.desktop', root / '.desktop' / 'after-effects'):
        if part.is_symlink():
            raise ValueError('After Effects host settings must not be a symlink')
        if create:
            part.mkdir(mode=0o700, exist_ok=True)
            part.chmod(0o700)
    return part


def _read_json(path, default=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Expected a regular After Effects receipt')
        value = handle.read(MAX_RECEIPT_BYTES + 1)
    if len(value) > MAX_RECEIPT_BYTES:
        raise ValueError('After Effects receipt exceeded its size limit')
    return json.loads(value)


def _write_json(path, value):
    if path.is_symlink():
        raise ValueError('After Effects state must not be a symlink')
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _settings(runs_dir=None):
    value = _read_json(_private_root(runs_dir, create=False) / 'settings.json', {})
    if not isinstance(value, dict):
        raise ValueError('Invalid After Effects host settings')
    return value


def _app_info(path):
    if not path.is_absolute() or path.suffix.lower() != '.app' or not path.is_dir():
        raise ValueError('Choose the installed Adobe After Effects .app folder')
    try:
        with (path / 'Contents' / 'Info.plist').open('rb') as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ValueError('This folder has no valid macOS application metadata') from exc
    if not str(info.get('CFBundleIdentifier', '')).lower().startswith('com.adobe.aftereffects'):
        raise ValueError('Choose Adobe After Effects, not another application')
    return {'path': str(path.resolve()), 'version': str(info.get('CFBundleShortVersionString', 'unknown'))}


def _discover(settings):
    selected = settings.get('app_path')
    if selected:
        try:
            app = _app_info(Path(selected))
        except ValueError:
            return None, None
    else:
        candidates = []
        for root in (Path('/Applications'), Path.home() / 'Applications'):
            candidates.extend(root.glob('*After Effects*.app'))
            candidates.extend(root.glob('*After Effects*/*.app'))
        apps = []
        for candidate in candidates:
            with contextlib.suppress(ValueError):
                apps.append(_app_info(candidate))
        apps.sort(key=lambda item: [int(n) for n in re.findall(r'\d+', item['version'])], reverse=True)
        app = apps[0] if apps else None
    render = None
    choices = [Path(settings['aerender_path'])] if settings.get('aerender_path') else (
        [Path(app['path']).parent / 'aerender', Path(app['path']) / 'Contents' / 'MacOS' / 'aerender'] if app else [])
    for candidate in choices:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            render = str(candidate.resolve())
            break
    return app, render


def _pending(root):
    active = _read_json(root / 'active.json')
    if not active:
        return None
    row = None
    try:
        row = _read_json(Path(active['job_file']))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        receipt = _read_json(Path(active['job_file']).parent / 'completion.json')
        if receipt and row and receipt.get('nonce') == row['nonce']:
            # Status is read-only. Only writers holding dispatch.lock may replace
            # or clear a lease, otherwise a late status for A could erase job B.
            return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    uncertain = not row or row['status'] in TERMINAL or time.time() - row.get('heartbeat', 0) > 45
    return {'job_id': active.get('job_id'), 'status': row.get('status', 'interrupted') if row else 'interrupted',
            'uncertain': uncertain,
            'message': ('A previous script may still be running in After Effects. Inspect the app and press Esc if needed. '
                        'Only clear this execution after confirming it has stopped.' if uncertain else
                        'A script is using After Effects. Wait for it to finish before submitting another script.')}


def settings_status(runs_dir=None):
    root = _private_root(runs_dir, create=False)
    settings = _settings(runs_dir)
    app, render = _discover(settings) if platform.system() == 'Darwin' else (None, None)
    enabled, supported = bool(settings.get('enabled', False)), platform.system() == 'Darwin'
    pending = _pending(root)
    state = 'ready' if enabled and app else 'disabled' if not enabled else 'unavailable'
    message = ('Local automation is enabled.' if enabled and app else
               'Enable local automation to run trusted JSX scripts.' if not enabled else
               'Install After Effects or choose its .app path in advanced settings.')
    if not supported:
        message = 'This native After Effects bridge currently supports macOS. Windows scripting is not implemented.'
    return {'id': 'after_effects', 'name': 'After Effects', 'kind': 'local', 'state': state,
            'enabled': enabled, 'supported': supported, 'available': bool(app), 'connected': enabled and bool(app),
            'app_path': app['path'] if app else settings.get('app_path'),
            'app_version': app['version'] if app else None, 'aerender_path': render,
            'can_render': enabled and bool(render), 'message': message, 'trust_notice': TRUST_NOTICE,
            'setup_steps': SETUP_STEPS, 'pending_execution': pending, 'persistent': True,
            'tools_count': 4 if enabled and app else 0}


def configure(body, runs_dir=None):
    """Human-facing host API. Never expose this function as an agent tool."""
    if not isinstance(body, dict) or set(body) - {'enabled', 'app_path', 'aerender_path', 'acknowledge_interrupted'}:
        raise ValueError('Unknown After Effects settings')
    for field in ('enabled', 'acknowledge_interrupted'):
        if field in body and type(body[field]) is not bool:
            raise ValueError(field + ' must be a boolean')
    root = _private_root(runs_dir)
    with FileLock(str(root / 'settings.lock')):
        settings = _settings(runs_dir)
        for field in ('app_path', 'aerender_path'):
            if field not in body:
                continue
            value = body[field]
            if value in (None, ''):
                settings.pop(field, None)
                continue
            if not isinstance(value, str) or any(ord(c) < 32 for c in value) or len(value) > 4096:
                raise ValueError('Enter a valid absolute ' + field)
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError(field + ' must be absolute')
            if field == 'app_path':
                _app_info(path)
            elif not path.is_file() or not os.access(path, os.X_OK) or path.name != 'aerender':
                raise ValueError('Choose the installed aerender executable')
            settings[field] = str(path.resolve())
        if 'enabled' in body:
            settings['enabled'] = body['enabled']
            settings['updated_at'] = time.time()
        _write_json(root / 'settings.json', settings)
    if body.get('acknowledge_interrupted'):
        with FileLock(str(root / 'dispatch.lock')):
            pending = _pending(root)
            if pending and not pending['uncertain']:
                raise ValueError('The script supervisor is still running. Cancel or wait before clearing it.')
            (root / 'active.json').unlink(missing_ok=True)
    return settings_status(runs_dir)


class AfterEffects:
    def __init__(self, runs_dir=None):
        self.runs_dir = runs_dir

    def status(self):
        return settings_status(self.runs_dir)

    def configure(self, body):
        return configure(body, self.runs_dir)


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value
            or any(ord(c) < 32 for c in value) or Path(value).is_absolute()
            or any(part in ('..', '.') for part in value.split('/'))):
        raise ValueError('Use a relative path inside the request work folder, without symlinks or ..')
    return Path(value)


@contextlib.contextmanager
def _work_parent(ws, relative, *, create=False):
    fd = os.open(ws.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=fd)
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _work_write(ws, value, content):
    """Publish review copies without following an agent-replaced work symlink."""
    relative = _relative(value)
    with _work_parent(ws, relative, create=True) as parent:
        temporary = '.' + uuid.uuid4().hex + '.tmp'
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(content)
            os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)


def _work_mkdir(ws, value):
    relative = _relative(value)
    with _work_parent(ws, relative, create=True) as parent:
        os.mkdir(relative.name, mode=0o700, dir_fd=parent)


def _work_file(ws, value, *, limit=None, max_bytes=1024 * 1024 * 1024):
    """Open relative work files using no-follow descriptors at every component."""
    relative = _relative(value)
    fd = os.open(ws.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Expected a regular workspace file')
            if info.st_size > max_bytes:
                raise ValueError('Workspace file exceeds the allowed size')
            if limit is not None:
                if info.st_size > limit:
                    raise ValueError('Workspace file exceeds the allowed size')
                return handle.read(limit + 1)
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            return {'path': relative.as_posix(), 'bytes': info.st_size, 'sha256': digest}
    finally:
        os.close(fd)


def _new_output(ws, value):
    relative = _relative(value)
    current = ws.work
    if current.is_symlink():
        raise ValueError('Workspace work folder must not be a symlink')
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Output paths must not contain symlinks')
    if current.exists():
        raise ValueError('Output already exists. Choose a new output path to preserve earlier files.')
    return current


def _job_root(ws):
    root = ws.root / '.after-effects'
    for path in (root, root / 'jobs'):
        if path.is_symlink():
            raise ValueError('After Effects job state must not be a symlink')
        path.mkdir(mode=0o700, exist_ok=True)
    return root / 'jobs'


def _timeout(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= maximum:
        raise ValueError(f'timeout_s must be between 1 and {maximum}')
    return float(value)


def _public(row):
    hidden = {'request_root', 'runs_dir', 'nonce', 'arguments', 'supervisor_pid', 'command_pid',
              'watchdog_pid', 'command_identity'}
    result = {key: value for key, value in row.items() if key not in hidden}
    result['authoring_target'] = 'after_effects'
    result['review'] = {'status': 'not_evaluated',
                        'reason': 'Native project/output; completion and file hashes do not evaluate design fidelity or approve delivery.'}
    if row['kind'] == 'render' and row['status'] == 'completed' and row.get('output'):
        path = row['output']['path']
        # media_register currently recognizes MOV/MP4 among AE's movie outputs.
        # Other native formats stay workspace files; do not offer a broken import.
        if Path(path).suffix.lower() in {'.mov', '.mp4'}:
            result['import_hint'] = {
                'tool': 'media_register', 'arguments': {'path': path},
                'note': 'Optional separate Registry call, subject to media.register policy and role permissions. '
                        'Imports rendered video as an immutable source, not editable AE layers or effects. '
                        'Keep the saved .aep and receipts for further native edits.',
            }
    return result


def update(directory, **changes):
    with FileLock(str(directory / 'job.lock')):
        row = _read_json(directory / 'job.json')
        if row['status'] in TERMINAL and changes.get('status', row['status']) != row['status']:
            changes.pop('status', None)
        row.update(changes)
        _write_json(directory / 'job.json', row)
    return row


def _enabled(runs_dir=None, *, render=False):
    status = settings_status(runs_dir)
    if not status['enabled']:
        raise ValueError('A person must enable After Effects local automation in Plugins and MCPs first. ' + TRUST_NOTICE)
    if not status['connected']:
        raise ValueError(status['message'])
    if render and not status['can_render']:
        raise ValueError('aerender was not found. Choose its installed path in After Effects advanced settings.')
    return status


def _environment():
    environment = {key: value for key, value in os.environ.items()
                   if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'USER', 'LOGNAME'}}
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    return environment


def process_identity(pid):
    """Pin PID start time plus full argv before allowing an owned-group kill."""
    try:
        result = subprocess.run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                                capture_output=True, text=True, timeout=3, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def launch(ws, owner, kind, arguments, runs_dir=None):
    if kind not in {'script', 'render'}:
        raise ValueError('Unknown After Effects operation')
    readiness = _enabled(runs_dir, render=kind == 'render')
    args = dict(arguments)
    timeout = _timeout(args.get('timeout_s', 300 if kind == 'script' else 1800), 1800 if kind == 'script' else 3600)
    payload = None
    if kind == 'script':
        source = _relative(args.get('script_path'))
        if source.suffix.lower() != '.jsx':
            raise ValueError('Use a plain UTF-8 .jsx script, not a compiled .jsxbin')
        payload = _work_file(ws, source.as_posix(), limit=MAX_SCRIPT_BYTES)
        payload.decode('utf-8-sig')
        if args.get('project_mode', 'isolated') not in {'isolated', 'current'}:
            raise ValueError('project_mode must be isolated or current')
        if args.get('project_path'):
            if args.get('project_mode') == 'current':
                raise ValueError('project_path cannot be combined with current project mode')
            if _relative(args['project_path']).suffix.lower() not in {'.aep', '.aepx', '.aet'}:
                raise ValueError('project_path must name an After Effects project')
            _work_file(ws, args['project_path'], max_bytes=512 * 1024 * 1024)
        expected = args.get('expected_outputs', [])
        if not isinstance(expected, list) or len(expected) > 32:
            raise ValueError('expected_outputs must contain at most 32 relative workspace paths')
        for path in expected:
            _new_output(ws, path)
    else:
        if _relative(args.get('project_path')).suffix.lower() not in {'.aep', '.aepx', '.aet'}:
            raise ValueError('project_path must name a workspace After Effects project')
        _work_file(ws, args['project_path'], max_bytes=512 * 1024 * 1024)
        output = _new_output(ws, args.get('output_path'))
        if output.suffix.lower() not in {'.mov', '.mp4', '.avi', '.mxf', '.wav', '.aiff', '.png', '.jpg', '.exr'}:
            raise ValueError('Choose a supported movie, audio or single-frame output filename')
        if any(c in str(output) for c in '[]%'):
            raise ValueError('Image sequence patterns are not supported; render a movie or a single frame')
        start, end = args.get('start_frame'), args.get('end_frame')
        if start is not None or end is not None:
            if type(start) is not int or type(end) is not int or not 0 <= start <= end <= 10000000:
                raise ValueError('Supply both start_frame and end_frame as ordered nonnegative frame numbers')
        if output.suffix.lower() in {'.png', '.jpg', '.exr'} and (start is None or start != end):
            raise ValueError('A still-image output requires equal start_frame and end_frame; sequences are not supported')
        comp, index = args.get('comp'), args.get('render_queue_index')
        if bool(comp) == (index is not None):
            raise ValueError('Choose exactly one comp name or render_queue_index')
        for field in ('comp', 'output_module', 'render_settings'):
            value = args.get(field)
            if value is not None and (not isinstance(value, str) or not value or len(value) > 1000 or '\0' in value):
                raise ValueError(field + ' must be a nonempty string')
        if index is not None and (type(index) is not int or not 1 <= index <= 10000):
            raise ValueError('render_queue_index must be a positive integer')
    root = _private_root(runs_dir)
    with FileLock(str(root / 'dispatch.lock')):
        if kind == 'script' and _pending(root):
            raise ValueError('After Effects already has an active or unresolved script. Inspect its status before running another.')
        jobs = _job_root(ws)
        rows = [_read_job(path) for path in jobs.iterdir() if JOB_ID.fullmatch(path.name)]
        if sum(row['status'] not in TERMINAL for row in rows) >= 2:
            raise ValueError('Two After Effects jobs are already active for this request. Wait or cancel one.')
        job_id = uuid.uuid4().hex
        directory = jobs / job_id
        directory.mkdir(mode=0o700)
        public_dir = _new_output(ws, f'after-effects/{job_id}')
        _work_mkdir(ws, f'after-effects/{job_id}')
        if payload is not None:
            (directory / 'script.jsx').write_bytes(payload)
            _work_write(ws, f'after-effects/{job_id}/script.jsx', payload)
        row = {'id': job_id, 'kind': kind, 'owner': owner, 'status': 'starting', 'created': time.time(),
               'heartbeat': time.time(), 'timeout_s': timeout, 'nonce': uuid.uuid4().hex,
               'request_root': str(ws.root.resolve()), 'workspace_path': str(ws.work.resolve()),
               'runs_dir': str(Path(runs_dir or config.RUNS_DIR).resolve()),
               'arguments': args, 'app_path': readiness['app_path'], 'aerender_path': readiness['aerender_path'],
               'output_directory': public_dir.relative_to(ws.work).as_posix(),
               'execution_uncertain': False,
               'note': ('Job continues across chat turns. No job is automatically replayed. '
                        'Script cancellation cannot forcibly stop JSX inside the desktop application.')}
        _write_json(directory / 'job.json', row)
        if kind == 'script':
            _write_json(root / 'active.json', {'job_id': job_id, 'job_file': str(directory / 'job.json')})
        environment = _environment()
        try:
            with (directory / 'supervisor.log').open('wb') as output:
                process = subprocess.Popen([sys.executable, '-m', 'vwb.desktop.worker', str(directory / 'job.json')],
                                           stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=environment,
                                           start_new_session=True, close_fds=True)
            row = update(directory, supervisor_pid=process.pid)
        except OSError:
            row = update(directory, status='failed', ended=time.time(), error='Could not launch the native supervisor.')
            if kind == 'script':
                (root / 'active.json').unlink(missing_ok=True)
            raise
    return _public(row)


def _read_job(directory):
    row = _read_json(directory / 'job.json')
    if row.get('execution_uncertain'):
        with contextlib.suppress(ValueError, OSError):
            receipt = _read_json(directory / 'completion.json')
            if receipt and receipt.get('nonce') == row['nonce']:
                row = update(directory, execution_uncertain=False,
                             late_completion={key: value for key, value in receipt.items() if key != 'nonce'})
    if row['status'] not in TERMINAL and time.time() - row['heartbeat'] > 45:
        (directory / 'cancel').touch()
        row = update(directory, status='interrupted', ended=time.time(), execution_uncertain=row['kind'] == 'script',
                     error='Supervisor stopped responding. Nothing was replayed; inspect the app and saved files before retrying.')
    return row


def job_status(ws, arguments):
    wait = arguments.get('wait_s', 0)
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= 30:
        raise ValueError('wait_s must be between 0 and 30')
    if type(arguments.get('cancel', False)) is not bool:
        raise ValueError('cancel must be a boolean')
    job_id = arguments.get('job_id')
    jobs = _job_root(ws)
    if not job_id:
        if arguments.get('cancel'):
            raise ValueError('Choose a job_id to cancel')
        rows = [_read_job(path) for path in jobs.iterdir() if JOB_ID.fullmatch(path.name)]
        rows.sort(key=lambda row: row['created'], reverse=True)
        return {'jobs': [_public(row) for row in rows[:100]], 'truncated': len(rows) > 100}
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or not (jobs / job_id / 'job.json').is_file():
        raise ValueError('After Effects job not found in this request')
    directory = jobs / job_id
    row = _read_job(directory)
    if arguments.get('cancel') and row['status'] not in TERMINAL:
        (directory / 'cancel').touch()
    deadline = time.monotonic() + wait
    while row['status'] not in TERMINAL and time.monotonic() < deadline:
        time.sleep(.1)
        row = _read_job(directory)
    result = _public(row)
    result['cancel_requested'] = (directory / 'cancel').exists()
    for stream in ('stdout', 'stderr'):
        offset = arguments.get(stream + '_offset', 0)
        if type(offset) is not int or offset < 0:
            raise ValueError(stream + '_offset must be a nonnegative integer')
        path = directory / (stream + '.log')
        with path.open('rb') if path.is_file() else contextlib.nullcontext(None) as handle:
            size = os.fstat(handle.fileno()).st_size if handle else 0
            if offset > size:
                raise ValueError(stream + '_offset exceeds the saved log size')
            if handle:
                handle.seek(offset)
            data = handle.read(16000) if handle else b''
        result[stream] = data.decode('utf-8', errors='replace')
        result[stream + '_next_offset'] = offset + len(data)
        result[stream + '_more'] = size > offset + len(data)
    return result


def _jsx_literal(value):
    return json.dumps(value, ensure_ascii=True)


def build_wrapper(row, directory):
    """ES3-compatible JSX with its own serializer; AE does not guarantee global JSON."""
    ws = Path(row['workspace_path'])
    args = row['arguments']
    project = ws / row['output_directory'] / 'project.aep'
    source_project = str(ws / args['project_path']) if args.get('project_path') else ''
    return '''(function () {
    function quote(s) { return '"' + String(s).replace(/[\\\\"\\x00-\\x1f]/g, function(c) {
        var m = { '"':'\\\\"', '\\\\':'\\\\\\\\', '\\n':'\\\\n', '\\r':'\\\\r', '\\t':'\\\\t' };
        return m[c] || ('\\\\u' + ('0000' + c.charCodeAt(0).toString(16)).slice(-4)); }) + '"'; }
    function encode(x, depth) {
        if (depth > 20) return quote('[depth limit]');
        if (x === null || typeof x === 'undefined') return 'null';
        if (typeof x === 'string') return quote(x.slice(0, 32000));
        if (typeof x === 'number') return isFinite(x) ? String(x) : 'null';
        if (typeof x === 'boolean') return x ? 'true' : 'false';
        var p = [], i, k;
        if (x instanceof Array) { for (i = 0; i < Math.min(x.length, 500); i++) p.push(encode(x[i], depth+1)); return '['+p.join(',')+']'; }
        if (typeof x === 'object') { i=0; for (k in x) { if (x.hasOwnProperty(k) && i++ < 200) p.push(quote(k)+':'+encode(x[k],depth+1)); } return '{'+p.join(',')+'}'; }
        return quote(String(x));
    }
    var receipt = {nonce: __NONCE__, status:'failed', logs:[], result:null};
    var file = new File(__RECEIPT__);
    file.encoding = 'UTF-8';
    if (!file.open('w')) throw new Error('Workbench cannot write its receipt. Enable Allow Scripts To Write Files And Access Network in After Effects Settings.');
    file.close();
    var prior = $.global.vwb;
    var undo = false, suppress = false;
    $.global.vwb = {workPath:__WORK__, outputPath:__OUTPUT__, projectPath:__PROJECT__, result:null,
        log:function(value) { if(receipt.logs.length<200) receipt.logs.push(String(value).slice(0,1000)); }};
    try {
        if (__MODE__ === 'isolated') {
            if (app.project && ((typeof app.project.dirty === 'boolean' && app.project.dirty) ||
                    (app.project.numItems > 0 && typeof app.project.dirty !== 'boolean')))
                throw new Error('Save your current After Effects project before starting a separate Workbench project. No unsaved changes were discarded.');
            if (app.project && app.project.isAnyTeamProjectOpen && app.project.isAnyTeamProjectOpen())
                throw new Error('Close the team project before starting a Workbench project.');
            if ((new File(__PROJECT__)).exists) throw new Error('The new Workbench project path already exists. Choose a new job.');
            app.beginSuppressDialogs(); suppress = true;
            if (app.project && !app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES))
                throw new Error('After Effects did not close the current saved project. No new project was opened.');
            var opened = __SOURCE_PROJECT__ ? app.open(new File(__SOURCE_PROJECT__)) : app.newProject();
            if (!opened || !app.project) throw new Error('After Effects did not open a project');
            app.project.save(new File(__PROJECT__));
        }
        app.beginUndoGroup('Video Workbench'); undo = true;
        var answer = $.evalFile(new File(__SCRIPT__));
        receipt.result = $.global.vwb.result === null ? answer : $.global.vwb.result;
        if (__MODE__ === 'isolated') app.project.save(new File(__PROJECT__));
        receipt.status = 'completed';
        receipt.project_path = __MODE__ === 'isolated' ? __PROJECT_REL__ : null;
    } catch (error) {
        receipt.error = String(error).slice(0,4000);
        receipt.line = error.line || null;
    } finally {
        if (undo) { try { app.endUndoGroup(); } catch (_) {} }
        if (suppress) { try { app.endSuppressDialogs(false); } catch (_) {} }
        $.global.vwb = prior;
        var encoded = encode(receipt, 0);
        if (encoded.length > 240000) { receipt.result = '[result exceeded receipt limit; write a file in work/]'; receipt.logs = receipt.logs.slice(0,40); encoded = encode(receipt,0); }
        if (!file.open('w')) throw new Error('Workbench could not save the completion receipt');
        file.write(encoded); file.close();
    }
    return receipt.status;
})();
'''.replace('__NONCE__', _jsx_literal(row['nonce'])).replace('__RECEIPT__', _jsx_literal(str(directory / 'completion.json'))).replace(
        '__WORK__', _jsx_literal(str(ws))).replace('__OUTPUT__', _jsx_literal(str(ws / row['output_directory']))).replace(
        '__PROJECT__', _jsx_literal(str(project))).replace('__MODE__', _jsx_literal(args.get('project_mode', 'isolated'))).replace(
        '__SOURCE_PROJECT__', _jsx_literal(source_project)).replace('__SCRIPT__', _jsx_literal(str(directory / 'script.jsx'))).replace(
        '__PROJECT_REL__', _jsx_literal(project.relative_to(ws).as_posix()))


def _apple_string(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def command(row, directory):
    if row['kind'] == 'script':
        script = directory / 'runner.jsx'
        script.write_text(build_wrapper(row, directory))
        ws = Workspace(Path(row['request_root']))
        _work_write(ws, row['output_directory'] + '/runner.jsx', script.read_bytes())
        apple = ('with timeout of ' + str(math.ceil(row['timeout_s'] + 10)) + ' seconds\n'
                 'tell application ' + _apple_string(row['app_path']) + '\n'
                 'DoScriptFile (POSIX file ' + _apple_string(str(script)) + ')\n'
                 'end tell\nend timeout')
        return ['/usr/bin/osascript', '-e', apple]
    args = row['arguments']
    root = Path(row['workspace_path'])
    output = root / args['output_path']
    output.parent.mkdir(parents=True, exist_ok=True)
    argv = [row['aerender_path'], '-project', str(root / args['project_path']), '-output', str(output)]
    argv += ['-comp', args['comp']] if args.get('comp') else ['-rqindex', str(args['render_queue_index'])]
    if args.get('output_module'):
        argv += ['-OMtemplate', args['output_module']]
    if args.get('render_settings'):
        argv += ['-RStemplate', args['render_settings']]
    if args.get('start_frame') is not None:
        argv += ['-s', str(args['start_frame']), '-e', str(args['end_frame'])]
    return argv


def _stop_owned(process):
    """Stop only our waiter/renderer group, never the user's GUI After Effects."""
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def execute_job(directory):
    """Called only by the detached trusted worker, with scrubbed inherited environment."""
    from vwb.tools.sandbox_tools import _Capture
    row = _read_json(directory / 'job.json')
    ws = Workspace(Path(row['request_root']))
    expected = _job_root(ws) / row['id']
    if directory.resolve() != expected.resolve() or directory.is_symlink():
        raise ValueError('Invalid After Effects job receipt path')
    process = None
    watchdog = None
    root = _private_root(row['runs_dir'])
    dispatched = False
    try:
        _enabled(row['runs_dir'], render=row['kind'] == 'render')
        if (directory / 'cancel').exists():
            update(directory, status='cancelled', ended=time.time())
            if row['kind'] == 'script':
                with FileLock(str(root / 'dispatch.lock')):
                    active = _read_json(root / 'active.json')
                    if active and active.get('job_id') == row['id']:
                        (root / 'active.json').unlink(missing_ok=True)
            return
        # Check submitted inputs again, including any parent-directory replacement.
        args = row['arguments']
        if args.get('project_path'):
            _work_file(ws, args['project_path'], max_bytes=512 * 1024 * 1024)
        if row['kind'] == 'render':
            _new_output(ws, args['output_path'])
        argv = command(row, directory)
        update(directory, status='running', started=time.time(), heartbeat=time.time())
        with (directory / 'stdout.log').open('wb') as stdout_log, (directory / 'stderr.log').open('wb') as stderr_log:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, close_fds=True, env=_environment())
            dispatched = True
            identity = process_identity(process.pid) if row['kind'] == 'render' else None
            update(directory, command_pid=process.pid, command_identity=identity)
            if row['kind'] == 'render' and not identity and process.poll() is None:
                raise ValueError('Could not pin the owned renderer process. Rendering was stopped before supervision could be lost.')
            if row['kind'] == 'render' and identity:
                # Independent liveness pipe: a killed supervisor cannot leave its
                # owned renderer running beyond cancellation/deadline indefinitely.
                with (directory / 'watchdog.log').open('wb') as watchdog_log:
                    watchdog = subprocess.Popen([sys.executable, '-m', 'vwb.desktop.watchdog',
                                                 str(directory / 'job.json')], stdin=subprocess.PIPE,
                                                stdout=watchdog_log, stderr=watchdog_log, env=_environment(),
                                                start_new_session=True, close_fds=True)
                update(directory, watchdog_pid=watchdog.pid)
            stdout, stderr = _Capture(process.stdout, stdout_log), _Capture(process.stderr, stderr_log)
            started, heartbeat = time.monotonic(), 0
            status = 'completed'
            while process.poll() is None:
                if (directory / 'cancel').exists() or not _settings(row['runs_dir']).get('enabled'):
                    status = 'cancelled'
                    break
                if time.monotonic() - started >= row['timeout_s']:
                    status = 'timed_out'
                    break
                if time.monotonic() - heartbeat >= 2:
                    update(directory, heartbeat=time.time())
                    heartbeat = time.monotonic()
                time.sleep(.1)
            if status != 'completed':
                _stop_owned(process)
            process.wait(timeout=5)
            out, err = stdout.text(), stderr.text()
        data = {'status': status, 'exit_code': process.returncode, 'ended': time.time(), 'heartbeat': time.time(),
                'stdout_bytes': stdout.total, 'stderr_bytes': stderr.total,
                'logs_truncated': max(stdout.total, stderr.total) > LOG_BYTES}
        if row['kind'] == 'script':
            with contextlib.suppress(ValueError, OSError, json.JSONDecodeError):
                receipt = _read_json(directory / 'completion.json')
                if receipt and receipt.get('nonce') == row['nonce']:
                    data.update({key: value for key, value in receipt.items() if key != 'nonce'})
                    data['execution_uncertain'] = False
            if 'execution_uncertain' not in data:
                data['execution_uncertain'] = dispatched
                if status == 'completed':
                    data['status'] = 'failed'
                data['error'] = ('No completion receipt was received. The script may still be running in After Effects. '
                                 'Inspect the app; enable script file/network access and macOS Automation permission if required. '
                                 + err[:2000])
            if data['status'] == 'completed' and args.get('project_mode', 'isolated') == 'isolated':
                data['project'] = _work_file(ws, row['output_directory'] + '/project.aep')
            if data['status'] == 'completed':
                data['outputs'] = [_work_file(ws, value) for value in args.get('expected_outputs', [])]
            if not data['execution_uncertain']:
                with FileLock(str(root / 'dispatch.lock')):
                    active = _read_json(root / 'active.json')
                    if active and active.get('job_id') == row['id']:
                        (root / 'active.json').unlink(missing_ok=True)
        elif data['status'] == 'completed':
            if process.returncode != 0:
                data.update(status='failed', error=err[:2000] or out[-2000:] or 'aerender exited with an error')
            else:
                data['output'] = _work_file(ws, args['output_path'])
                if not data['output']['bytes']:
                    raise ValueError('aerender completed without a nonempty output file')
        update(directory, **data)
        public = _public(_read_json(directory / 'job.json'))
        _work_write(ws, row['output_directory'] + '/receipt.json', json.dumps(public).encode('utf-8'))
    except Exception as exc:
        update(directory, status='failed', ended=time.time(), heartbeat=time.time(), error=str(exc)[:4000],
               execution_uncertain=bool(dispatched and row['kind'] == 'script'))
        if row['kind'] == 'script' and not dispatched:
            with FileLock(str(root / 'dispatch.lock')):
                active = _read_json(root / 'active.json')
                if active and active.get('job_id') == row['id']:
                    (root / 'active.json').unlink(missing_ok=True)
    finally:
        if process:
            _stop_owned(process)
        if watchdog:
            watchdog.stdin.close()
            with contextlib.suppress(subprocess.TimeoutExpired):
                watchdog.wait(timeout=5)
