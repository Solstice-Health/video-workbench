"""Native creative automation is opt-in, durable, bounded and never kills the GUI app."""

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vwb.desktop import after_effects as ae


@pytest.fixture
def native(tmp_path, monkeypatch):
    runs = tmp_path / 'runs'
    request = runs / 'request'
    work = request / 'work'
    work.mkdir(parents=True)
    (work / 'build.jsx').write_text('vwb.result = {message: "hello"};')
    app = tmp_path / 'Adobe After Effects 2026' / 'Adobe After Effects 2026.app'
    (app / 'Contents').mkdir(parents=True)
    with (app / 'Contents/Info.plist').open('wb') as handle:
        plistlib.dump({'CFBundleIdentifier': 'com.adobe.AfterEffects', 'CFBundleShortVersionString': '26.0'}, handle)
    renderer = app.parent / 'aerender'
    renderer.write_text('#!/bin/sh\nexit 0\n')
    renderer.chmod(0o755)
    monkeypatch.setattr(ae.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(ae.config, 'RUNS_DIR', runs)
    ws = SimpleNamespace(root=request, work=work)
    return SimpleNamespace(runs=runs, ws=ws, app=app, renderer=renderer)


def enable(native):
    return ae.configure({'enabled': True, 'app_path': str(native.app)}, native.runs)


def stub_launch(native, monkeypatch, *, kind='script', args=None):
    enable(native)
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=123456)

    with monkeypatch.context() as scoped:
        scoped.setattr(ae.subprocess, 'Popen', popen)
        row = ae.launch(native.ws, 'job_lead', kind, args or {'script_path': 'build.jsx'}, native.runs)
    return row, calls


def job_dir(native, row):
    return native.ws.root / '.after-effects/jobs' / row['id']


def test_settings_disabled_by_default_and_do_not_start_app(native, monkeypatch):
    monkeypatch.setattr(ae, '_discover', lambda settings: ({'path': str(native.app), 'version': '26'}, None))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('Status must not launch anything'))
    result = ae.AfterEffects(native.runs).status()
    assert result['enabled'] is False
    assert result['connected'] is False
    assert result['state'] == 'disabled'
    assert 'not a security sandbox' in result['trust_notice']
    assert not result['pending_execution']


def test_human_configuration_persistent_and_private(native):
    result = enable(native)
    assert result['connected'] and result['can_render']
    assert result['app_version'] == '26.0'
    settings = native.runs / '.desktop/after-effects/settings.json'
    assert settings.stat().st_mode & 0o777 == 0o600
    assert ae.AfterEffects(native.runs).status()['enabled']
    assert not ae.configure({'enabled': False}, native.runs)['connected']


@pytest.mark.parametrize('body', [{'enabled': 'true'}, {'acknowledge_interrupted': 1}, {'command': 'x'},
                                 {'app_path': 'relative.app'}, {'aerender_path': '/bin/sh'}])
def test_configuration_rejects_invalid_values(native, body):
    with pytest.raises(ValueError):
        ae.configure(body, native.runs)


def test_non_adobe_application_rejected(native):
    with (native.app / 'Contents/Info.plist').open('wb') as handle:
        plistlib.dump({'CFBundleIdentifier': 'org.unrelated.App'}, handle)
    with pytest.raises(ValueError, match='Adobe After Effects'):
        enable(native)


def test_platform_limit_actionable(native, monkeypatch):
    monkeypatch.setattr(ae.platform, 'system', lambda: 'Linux')
    result = enable(native)
    assert not result['connected'] and not result['supported']
    assert 'macOS' in result['message']


def test_agent_cannot_enable_local_automation(native):
    with pytest.raises(ValueError, match='person must enable'):
        ae.launch(native.ws, 'job_lead', 'script', {'script_path': 'build.jsx'}, native.runs)
    from vwb.tools.after_effects_tools import TOOLS
    assert {tool.name for tool in TOOLS} == {'after_effects_status', 'after_effects_run',
                                          'after_effects_render', 'after_effects_job'}
    assert all('configure' not in tool.name for tool in TOOLS)
    assert next(tool for tool in TOOLS if tool.name == 'after_effects_job').capability.endswith('.execute')


@pytest.mark.parametrize('path', ['/tmp/a.jsx', '../outside.jsx', 'scripts/../a.jsx', './a.jsx',
                                 'a\\b.jsx', 'a\x00.jsx', ''])
def test_script_paths_are_workspace_relative(native, path):
    enable(native)
    with pytest.raises(ValueError):
        ae.launch(native.ws, 'job_lead', 'script', {'script_path': path}, native.runs)


def test_symlink_script_and_parent_rejected(native, tmp_path):
    enable(native)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'payload.jsx').write_text('alert(1)')
    (native.ws.work / 'linked.jsx').symlink_to(outside / 'payload.jsx')
    (native.ws.work / 'linked-dir').symlink_to(outside, target_is_directory=True)
    for path in ('linked.jsx', 'linked-dir/payload.jsx'):
        with pytest.raises((ValueError, OSError)):
            ae.launch(native.ws, 'job_lead', 'script', {'script_path': path}, native.runs)


def test_oversized_script_and_compiled_script_rejected(native):
    enable(native)
    (native.ws.work / 'large.jsx').write_bytes(b'x' * (ae.MAX_SCRIPT_BYTES + 1))
    (native.ws.work / 'binary.jsxbin').write_bytes(b'binary')
    for path in ('large.jsx', 'binary.jsxbin'):
        with pytest.raises(ValueError):
            ae.launch(native.ws, 'job_lead', 'script', {'script_path': path}, native.runs)


def test_job_receipt_precedes_dispatch_and_payload_is_pinned(native, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-private-value')
    row, calls = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    stored = json.loads((directory / 'job.json').read_text())
    assert stored['status'] == 'starting' and stored['supervisor_pid'] == 123456
    assert calls[0][0][1:3] == ['-m', 'vwb.desktop.worker']
    assert 'ANTHROPIC_API_KEY' not in calls[0][1]['env']
    assert calls[0][1]['start_new_session']
    assert row['workspace_path'] == str(native.ws.work.resolve())
    assert 'nonce' not in row and 'arguments' not in row
    assert 'request_root' not in row
    (native.ws.work / 'build.jsx').write_text('replaced after submission')
    assert (directory / 'script.jsx').read_text() == 'vwb.result = {message: "hello"};'


def test_global_gui_dispatch_lease_prevents_overlap_and_live_ack(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    assert ae.settings_status(native.runs)['pending_execution']['job_id'] == row['id']
    with pytest.raises(ValueError, match='active or unresolved'):
        ae.launch(native.ws, 'job_lead', 'script', {'script_path': 'build.jsx'}, native.runs)
    with pytest.raises(ValueError, match='still running'):
        ae.configure({'acknowledge_interrupted': True}, native.runs)


def test_interrupted_script_never_replayed_and_human_can_ack(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    ae.update(directory, heartbeat=time.time() - 50)
    result = ae.job_status(native.ws, {'job_id': row['id']})
    assert result['status'] == 'interrupted' and result['execution_uncertain']
    pending = ae.settings_status(native.runs)['pending_execution']
    assert pending['uncertain']
    assert ae.configure({'acknowledge_interrupted': True}, native.runs)['pending_execution'] is None
    assert ae.job_status(native.ws, {'job_id': row['id']})['status'] == 'interrupted'


def test_late_completion_clears_lease_without_replaying(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    private = ae.update(directory, status='cancelled', execution_uncertain=True)
    ae._write_json(directory / 'completion.json', {'nonce': private['nonce'], 'status': 'completed', 'result': {'ok': 1}})
    assert ae.settings_status(native.runs)['pending_execution'] is None
    result = ae.job_status(native.ws, {'job_id': row['id']})
    assert result['status'] == 'cancelled'
    assert result['execution_uncertain'] is False
    assert result['late_completion']['result'] == {'ok': 1}


def test_spoofed_completion_does_not_clear_lease(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    ae._write_json(job_dir(native, row) / 'completion.json', {'nonce': 'wrong', 'status': 'completed'})
    assert ae.settings_status(native.runs)['pending_execution']


def test_empty_inflight_receipt_does_not_make_live_lease_uncertain(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    ae.update(directory, status='running', heartbeat=time.time())
    (directory / 'completion.json').touch()
    pending = ae.settings_status(native.runs)['pending_execution']
    assert pending['status'] == 'running' and not pending['uncertain']
    with pytest.raises(ValueError, match='still running'):
        ae.configure({'acknowledge_interrupted': True}, native.runs)


def test_status_does_not_mutate_completed_dispatch_lease(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    private = json.loads((directory / 'job.json').read_text())
    ae._write_json(directory / 'completion.json', {'nonce': private['nonce'], 'status': 'completed'})
    active = native.runs / '.desktop/after-effects/active.json'
    before = active.read_bytes()
    assert ae.settings_status(native.runs)['pending_execution'] is None
    assert active.read_bytes() == before


@pytest.mark.parametrize('timeout', [0, True, -1, 1801, float('nan'), float('inf')])
def test_runtime_bounds(native, timeout):
    enable(native)
    with pytest.raises(ValueError, match='timeout_s'):
        ae.launch(native.ws, 'job_lead', 'script', {'script_path': 'build.jsx', 'timeout_s': timeout}, native.runs)


def test_current_mode_cannot_implicitly_open_another_project(native):
    enable(native)
    with pytest.raises(ValueError, match='cannot be combined'):
        ae.launch(native.ws, 'job_lead', 'script', {'script_path': 'build.jsx', 'project_mode': 'current',
                                                'project_path': 'existing.aep'}, native.runs)


def test_wrapper_uses_safe_default_project_and_jsx_globals(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    private = json.loads((directory / 'job.json').read_text())
    wrapper = ae.build_wrapper(private, directory)
    assert 'app.project.dirty' in wrapper
    assert 'No unsaved changes were discarded' in wrapper
    assert wrapper.index('app.project.dirty') < wrapper.index('app.project.close(')
    assert 'CloseOptions.DO_NOT_SAVE_CHANGES' in wrapper
    assert 'vwb' in wrapper and 'workPath:' in wrapper and 'outputPath:' in wrapper
    assert 'JSON.stringify' not in wrapper
    assert str(native.ws.work) in wrapper
    command = ae.command(private, directory)
    assert command[0:2] == ['/usr/bin/osascript', '-e']
    assert 'DoScriptFile (POSIX file ' in command[2]
    assert private['app_path'] in command[2]


def test_apple_script_path_quoting_stays_in_single_string():
    value = '/Applications/After "Effects" \\ Test.app'
    assert ae._apple_string(value) == '"/Applications/After \\"Effects\\" \\\\ Test.app"'


def test_render_validates_output_and_never_reuses_gui(native, monkeypatch):
    (native.ws.work / 'source.aep').write_bytes(b'project')
    args = {'project_path': 'source.aep', 'output_path': 'renders/new.mov', 'comp': 'Main',
            'output_module': 'Lossless', 'render_settings': 'Best Settings'}
    row, _ = stub_launch(native, monkeypatch, kind='render', args=args)
    directory = job_dir(native, row)
    command = ae.command(json.loads((directory / 'job.json').read_text()), directory)
    assert command[0] == str(native.renderer)
    assert '-reuse' not in command
    assert command[command.index('-comp') + 1] == 'Main'
    assert command[command.index('-output') + 1] == str(native.ws.work / 'renders/new.mov')
    (native.ws.work / 'renders/new.mov').write_bytes(b'old')
    with pytest.raises(ValueError, match='already exists'):
        ae.launch(native.ws, 'job_lead', 'render', args, native.runs)


@pytest.mark.parametrize('args', [
    {'comp': 'A', 'render_queue_index': 1}, {}, {'render_queue_index': True},
    {'comp': 'A', 'output_path': 'frames/frame_[####].png'}, {'comp': 'A', 'output_path': '../outside.mov'},
])
def test_invalid_render_arguments(native, args):
    enable(native)
    (native.ws.work / 'source.aep').write_bytes(b'project')
    with pytest.raises(ValueError):
        ae.launch(native.ws, 'job_lead', 'render',
                  {'project_path': 'source.aep', 'output_path': 'out.mov', **args}, native.runs)


def test_script_worker_collects_receipt_and_checks_output(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch, args={'script_path': 'build.jsx', 'expected_outputs': ['result.txt']})
    directory = job_dir(native, row)
    private = json.loads((directory / 'job.json').read_text())
    receipt = {'nonce': private['nonce'], 'status': 'completed', 'result': {'layers': 3},
               'project_path': row['output_directory'] + '/project.aep'}
    script = ('import pathlib,json; '
              f'pathlib.Path({str(directory / "completion.json")!r}).write_text({json.dumps(receipt)!r}); '
              f'pathlib.Path({str(native.ws.work / row["output_directory"] / "project.aep")!r}).write_bytes(b"project"); '
              f'pathlib.Path({str(native.ws.work / "result.txt")!r}).write_text("result"); print("done")')
    monkeypatch.setattr(ae, 'command', lambda r, d: [sys.executable, '-c', script])
    ae.execute_job(directory)
    result = ae.job_status(native.ws, {'job_id': row['id']})
    assert result['status'] == 'completed' and result['execution_uncertain'] is False
    assert result['project']['bytes'] == 7
    assert result['outputs'][0]['path'] == 'result.txt'
    assert result['result']['layers'] == 3
    assert result['stdout'].strip() == 'done'
    assert ae.settings_status(native.runs)['pending_execution'] is None
    public = json.loads((native.ws.work / row['output_directory'] / 'receipt.json').read_text())
    assert 'nonce' not in public and 'arguments' not in public


def test_renderer_success_requires_nonempty_output(native, monkeypatch):
    (native.ws.work / 'source.aep').write_bytes(b'project')
    row, _ = stub_launch(native, monkeypatch, kind='render',
                         args={'project_path': 'source.aep', 'output_path': 'result.mov', 'comp': 'Main'})
    monkeypatch.setattr(ae, 'command', lambda r, d: [sys.executable, '-c', 'print("no output")'])
    ae.execute_job(job_dir(native, row))
    result = ae.job_status(native.ws, {'job_id': row['id']})
    assert result['status'] == 'failed'
    assert 'No such file' in result['error']


def test_no_completion_is_uncertain_and_does_not_kill_gui(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    monkeypatch.setattr(ae, 'command', lambda r, d: [sys.executable, '-c', 'print("no receipt")'])
    monkeypatch.setattr(ae.os, 'killpg', lambda *args: pytest.fail('Finished waiter should not kill anything'))
    ae.execute_job(job_dir(native, row))
    result = ae.job_status(native.ws, {'job_id': row['id']})
    assert result['status'] == 'failed' and result['execution_uncertain']
    assert ae.settings_status(native.runs)['pending_execution']['uncertain']


def test_cancel_before_dispatch_does_not_start_app(native, monkeypatch):
    row, _ = stub_launch(native, monkeypatch)
    directory = job_dir(native, row)
    (directory / 'cancel').touch()
    monkeypatch.setattr(ae.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Cancelled before dispatch'))
    ae.execute_job(directory)
    result = ae.job_status(native.ws, {'job_id': row['id']})
    assert result['status'] == 'cancelled' and not result['execution_uncertain']


def test_unknown_job_and_invalid_offsets_are_rejected(native, monkeypatch):
    with pytest.raises(ValueError, match='not found'):
        ae.job_status(native.ws, {'job_id': 'a' * 32})
    row, _ = stub_launch(native, monkeypatch)
    with pytest.raises(ValueError, match='nonnegative'):
        ae.job_status(native.ws, {'job_id': row['id'], 'stdout_offset': -1})
    with pytest.raises(ValueError, match='exceeds'):
        ae.job_status(native.ws, {'job_id': row['id'], 'stdout_offset': 1})


def test_host_settings_symlink_is_rejected(native, tmp_path):
    root = native.runs / '.desktop'
    root.mkdir()
    outside = tmp_path / 'external-settings'
    outside.mkdir()
    (root / 'after-effects').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        ae.settings_status(native.runs)


def test_review_copies_cannot_follow_replaced_workspace_directory(native, tmp_path):
    outside = tmp_path / 'outside-output'
    outside.mkdir()
    (native.ws.work / 'after-effects').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        ae._work_write(native.ws, 'after-effects/job/receipt.json', b'no')
    assert not list(outside.iterdir())


def test_single_frame_preview_requires_frame_bounds_and_passes_them(native, monkeypatch):
    enable(native)
    (native.ws.work / 'source.aep').write_bytes(b'project')
    args = {'project_path': 'source.aep', 'output_path': 'frame.png', 'comp': 'Main'}
    with pytest.raises(ValueError, match='equal start_frame'):
        ae.launch(native.ws, 'job_lead', 'render', args, native.runs)
    row, _ = stub_launch(native, monkeypatch, kind='render', args={**args, 'start_frame': 12, 'end_frame': 12})
    directory = job_dir(native, row)
    argv = ae.command(ae._read_json(directory / 'job.json'), directory)
    assert argv[-4:] == ['-s', '12', '-e', '12']


def test_agent_status_uses_injected_host_configuration(native, monkeypatch, tmp_path):
    from vwb.tools.after_effects_tools import status

    enable(native)
    monkeypatch.setattr(ae.config, 'RUNS_DIR', tmp_path / 'other-runs')
    ctx = SimpleNamespace(ws=native.ws, state={'integration_service': SimpleNamespace(after_effects=ae.AfterEffects(native.runs))})
    result = status(ctx, {})
    assert result.data['connected']
    assert result.data['workspace_path'] == str(native.ws.work.resolve())
    assert result.data['target']['id'] == 'after_effects'
    assert result.data['target']['availability']['status'] == 'available'


def test_actual_detached_worker_entrypoint_renders_and_finishes(native, monkeypatch):
    """Exercise the real launcher/argv/receipt/worker path, without Adobe or paid work."""
    enable(native)
    (native.ws.work / 'source.aep').write_bytes(b'project fixture')
    native.renderer.write_text(f'#!{sys.executable}\nimport pathlib,sys\n'
                               'pathlib.Path(sys.argv[sys.argv.index("-output")+1]).write_bytes(b"render fixture")\n')
    native.renderer.chmod(0o755)
    real_popen = subprocess.Popen
    workers = []

    def launch_worker(argv, **kwargs):
        # A synthetic installation must work on Linux CI too; patch only the
        # subprocess test's platform probe, not production enablement checks.
        if argv[1:3] != ['-m', 'vwb.desktop.worker']:
            return real_popen(argv, **kwargs)
        code = ('import platform,runpy,sys; platform.system=lambda:"Darwin"; '
                'sys.argv=["vwb.desktop.worker",sys.argv[1]]; '
                'runpy.run_module("vwb.desktop.worker",run_name="__main__")')
        process = real_popen([argv[0], '-c', code, argv[3]], **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(ae.subprocess, 'Popen', launch_worker)
    row = ae.launch(native.ws, 'job_lead', 'render',
                    {'project_path': 'source.aep', 'output_path': 'finished.mov', 'comp': 'Main'}, native.runs)
    try:
        result = ae.job_status(native.ws, {'job_id': row['id'], 'wait_s': 15})
        assert result['status'] == 'completed', result
        assert result['output']['bytes'] == len(b'render fixture')
        assert (native.ws.work / 'finished.mov').read_bytes() == b'render fixture'
        assert (native.ws.work / row['output_directory'] / 'receipt.json').is_file()
    finally:
        for process in workers:
            process.wait(timeout=5)


def test_renderer_watchdog_stops_owned_render_when_supervisor_is_killed(native, monkeypatch):
    enable(native)
    (native.ws.work / 'source.aep').write_bytes(b'project fixture')
    native.renderer.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n')
    native.renderer.chmod(0o755)
    real_popen = subprocess.Popen
    workers = []

    def launch_worker(argv, **kwargs):
        if argv[1:3] != ['-m', 'vwb.desktop.worker']:
            return real_popen(argv, **kwargs)
        code = ('import platform,runpy,sys; platform.system=lambda:"Darwin"; '
                'sys.argv=["vwb.desktop.worker",sys.argv[1]]; '
                'runpy.run_module("vwb.desktop.worker",run_name="__main__")')
        process = real_popen([argv[0], '-c', code, argv[3]], **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(ae.subprocess, 'Popen', launch_worker)
    row = ae.launch(native.ws, 'job_lead', 'render',
                    {'project_path': 'source.aep', 'output_path': 'slow.mov', 'comp': 'Main'}, native.runs)
    directory = job_dir(native, row)
    child_pid = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            private = ae._read_json(directory / 'job.json')
            if private.get('watchdog_pid'):
                child_pid = private['command_pid']
                break
            time.sleep(.05)
        assert child_pid, (directory / 'supervisor.log').read_text()
        assert ae.process_identity(child_pid) == private['command_identity']
        os.kill(workers[0].pid, signal.SIGKILL)
        workers[0].wait(timeout=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and ae.process_identity(child_pid) == private['command_identity']:
            time.sleep(.1)
        assert ae.process_identity(child_pid) != private['command_identity'], 'Owned renderer survived its supervisor and watchdog'
    finally:
        for process in workers:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        if child_pid and ae.process_identity(child_pid):
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('dirty,team,mode,expected', [
    (False, False, 'isolated', 'completed'),
    (True, False, 'isolated', 'failed'),
    (False, True, 'isolated', 'failed'),
    (True, False, 'current', 'completed'),
])
def test_jsx_wrapper_executes_in_es_vm_and_preserves_unsaved_projects(native, monkeypatch, dirty, team, mode, expected):
    node = shutil.which('node')
    fallback = Path('/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node')
    if not node and fallback.is_file():
        node = str(fallback)
    if not node:
        pytest.skip('Node is required for JSX wrapper semantic tests')
    row, _ = stub_launch(native, monkeypatch, args={'script_path': 'build.jsx', 'project_mode': mode})
    directory = job_dir(native, row)
    private = ae._read_json(directory / 'job.json')
    script = r'''
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const files = {}, events = [];
function File(path) { this.path = path; this.exists = Object.hasOwn(files, path); }
File.prototype.open = function() { files[this.path] = ''; return true; };
File.prototype.close = function() {};
File.prototype.write = function(s) { files[this.path] += String(s); };
function project() { return {dirty: input.dirty, numItems:1,
  isAnyTeamProjectOpen() { return input.team; },
  close() { events.push('close'); context.app.project = null; return true; },
  save(file) { events.push('save'); files[file.path]='project'; }}; }
const context = vm.createContext({File, CloseOptions:{DO_NOT_SAVE_CHANGES:0}});
context.app = {project:project(), beginSuppressDialogs(){events.push('suppress');}, endSuppressDialogs(){},
  beginUndoGroup(){}, endUndoGroup(){}, newProject(){events.push('new'); return this.project=project();},
  open(){events.push('open'); return this.project=project();}};
context.$ = {global:context, evalFile() {events.push('payload'); return vm.runInContext(
  'vwb.log("asset \\"one\\"\\n"); vwb.result={layers:3,label:"A\\nB",unicode:"étude"};',context);}};
vm.runInContext(input.wrapper,context,{timeout:1000});
const receipt = JSON.parse(files[input.receiptPath]);
process.stdout.write(JSON.stringify({receipt,events}));
'''
    data = {'wrapper': ae.build_wrapper(private, directory), 'dirty': dirty, 'team': team,
            'receiptPath': str(directory / 'completion.json')}
    result = subprocess.run([node, '-e', script], input=json.dumps(data), capture_output=True, text=True,
                            timeout=10, check=True)
    output = json.loads(result.stdout)
    assert output['receipt']['status'] == expected, output
    if expected == 'failed':
        assert 'close' not in output['events'] and 'payload' not in output['events']
    else:
        assert output['receipt']['result'] == {'layers': 3, 'label': 'A\nB', 'unicode': 'étude'}
        if mode == 'current':
            assert 'close' not in output['events'] and 'save' not in output['events']
        else:
            assert output['events'].count('save') == 2


def native_context(native):
    from vwb.tools import build_registry
    from vwb.tools.base import ToolContext
    from vwb.workspace import Workspace
    from vwb.workspace.request import SUBDIRS, Request

    ws = Workspace(native.ws.root)
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message='Author in native AE'))
    ctx = ToolContext(ws, 'job_lead', 'job_lead', None,
                      state={'integration_service': SimpleNamespace(after_effects=ae.AfterEffects(native.runs))})
    return build_registry(), ctx


def test_native_commands_use_recorded_shared_project_workspace(native, monkeypatch, tmp_path):
    from vwb.workspace import Workspace

    shared = tmp_path / 'shared-project'
    work = shared / 'work'
    work.mkdir(parents=True)
    (work / 'build.jsx').write_text('vwb.result = {comp:"Main"};')
    (work / 'existing.aep').write_bytes(b'existing project')
    native.ws.work = work
    monkeypatch.setattr(Workspace, 'project_root', property(lambda _: shared))
    row, _ = stub_launch(native, monkeypatch, args={'script_path': 'build.jsx', 'project_path': 'existing.aep'})
    directory = job_dir(native, row)
    private = ae._read_json(directory / 'job.json')
    wrapper = ae.build_wrapper(private, directory)
    assert str(work / 'existing.aep') in wrapper
    assert str(work / row['output_directory'] / 'project.aep') in wrapper
    assert str(native.ws.root / 'work') not in wrapper
    ae.command(private, directory)
    assert (work / row['output_directory'] / 'runner.jsx').is_file()
    rendered, _ = stub_launch(native, monkeypatch, kind='render', args={
        'project_path': 'existing.aep', 'output_path': 'renders/shared.mov', 'comp': 'Main'})
    render_dir = job_dir(native, rendered)
    argv = ae.command(ae._read_json(render_dir / 'job.json'), render_dir)
    assert argv[argv.index('-project') + 1] == str(work / 'existing.aep')
    assert argv[argv.index('-output') + 1] == str(work / 'renders/shared.mov')
    assert (work / 'existing.aep').read_bytes() == b'existing project'


def test_native_target_keeps_registry_policy_role_and_human_enablement_gates(native, monkeypatch):
    registry, ctx = native_context(native)
    monkeypatch.setattr(ae.subprocess, 'Popen', lambda *a, **k: pytest.fail('Blocked calls cannot dispatch'))
    args = {'script_path': 'build.jsx'}
    host = registry.call(ctx, 'after_effects_status', {})
    assert not host.is_error and not host.data['enabled']
    disabled = registry.call(ctx, 'after_effects_run', args)
    assert disabled.is_error and 'person must enable' in disabled.text
    enable(native)
    ctx.role = 'critic'
    assert registry.call(ctx, 'after_effects_run', args).is_error
    ctx.role = 'job_lead'
    ctx.state['allowed_tools'] = ['after_effects_status']
    assert 'inherited' in registry.call(ctx, 'after_effects_run', args).text
    del ctx.state['allowed_tools']
    monkeypatch.setattr(registry, 'mode', lambda cap: 'block' if cap == 'desktop.after_effects.execute' else 'allow')
    assert 'policy dial' in registry.call(ctx, 'after_effects_run', args).text
    monkeypatch.setattr(registry, 'mode', lambda cap: 'ask' if cap == 'desktop.after_effects.execute' else 'allow')
    parked = registry.call(ctx, 'after_effects_run', args)
    assert parked.data['parked']
    assert not (ctx.ws.root / '.after-effects/jobs').exists()


def test_registry_native_author_render_import_roundtrip_with_simulated_host(native, monkeypatch):
    """Simulated Adobe host; real jobs, movie encoding/decoding and immutable import.

    No Adobe app, license, preferences, OS consent, JSX engine or visual fidelity
    is validated here. Existing ES VM tests separately exercise the JSX wrapper.
    """
    from vwb.ask.broker import Broker
    from vwb.media.probe import probe

    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg and ffprobe are required for native movie handoff testing')
    registry, ctx = native_context(native)
    enable(native)
    settings = native.runs / '.desktop/after-effects/settings.json'
    settings_before = settings.read_bytes()
    monkeypatch.setattr(Broker, 'ask', lambda *a, **k: pytest.fail('Existing configuration and allow policy need no new approval'))

    def call(name, args):
        result = registry.call(ctx, name, args)
        assert not result.is_error, result.text
        return result

    target = call('after_effects_status', {}).data['target']
    assert target['availability']['status'] == 'available'
    assert target['kind'] == 'native_project'
    call('file_write', {'path': 'native/build.jsx', 'content': 'vwb.result = {comp: "Main"};'})

    # Submit through Registry and execute the trusted supervisor synchronously
    # so the test can substitute just the native application process.
    with monkeypatch.context() as scoped:
        scoped.setattr(ae.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(pid=123456))
        authored = call('after_effects_run', {'script_path': 'native/build.jsx'}).data
    directory = job_dir(native, authored)
    real_command = ae.command

    def simulated_author(row, folder):
        real_command(row, folder)  # Retain the actual runner.jsx and AppleScript contract.
        project_path = row['output_directory'] + '/project.aep'
        receipt = {'nonce': row['nonce'], 'status': 'completed', 'project_path': project_path,
                   'result': {'comp': 'Main'}}
        code = ('from pathlib import Path; '
                f'Path({str(ctx.ws.work / project_path)!r}).write_bytes(b"simulated AE project"); '
                f'Path({str(folder / "completion.json")!r}).write_text({json.dumps(receipt)!r})')
        return [sys.executable, '-c', code]

    with monkeypatch.context() as scoped:
        scoped.setattr(ae, 'command', simulated_author)
        ae.execute_job(directory)
    authored = call('after_effects_job', {'job_id': authored['id']}).data
    assert authored['status'] == 'completed'
    assert authored['review']['status'] == 'not_evaluated'
    project = ctx.ws.work / authored['project']['path']
    project_before = project.read_bytes()
    assert (ctx.ws.work / authored['output_directory'] / 'runner.jsx').is_file()

    # Installed aerender fixture accepts the real argv and creates an actual
    # six-frame movie; it does not render the dummy AE project.
    native.renderer.write_text(f'#!{sys.executable}\nimport subprocess,sys\n'
                               'output=sys.argv[sys.argv.index("-output")+1]\n'
                               f'subprocess.run([{ffmpeg!r}, "-nostdin", "-v", "error", "-f", "lavfi", '
                               '"-i", "testsrc2=size=64x48:rate=24", "-frames:v", "6", '
                               '"-c:v", "libx264", "-pix_fmt", "yuv420p", output], check=True)\n')
    native.renderer.chmod(0o755)
    with monkeypatch.context() as scoped:
        scoped.setattr(ae.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(pid=123456))
        render = call('after_effects_render', {'project_path': authored['project']['path'],
                                              'comp': authored['result']['comp'],
                                              'output_path': 'native/render-v1.mp4'}).data
    ae.execute_job(job_dir(native, render))
    render = call('after_effects_job', {'job_id': render['id']}).data
    assert render['status'] == 'completed', render
    assert render['review']['status'] == 'not_evaluated'
    assert project.read_bytes() == project_before
    assert not ctx.ws.load().sources  # Polling a job never imports implicitly.
    receipt = json.loads((ctx.ws.work / render['output_directory'] / 'receipt.json').read_text())
    assert receipt['import_hint'] == render['import_hint']
    hint = render['import_hint']
    with monkeypatch.context() as scoped:
        scoped.setattr(registry, 'mode', lambda cap: 'block' if cap == 'media.register' else 'allow')
        assert registry.call(ctx, hint['tool'], hint['arguments']).is_error
        assert not ctx.ws.load().sources
    imported = call(hint['tool'], hint['arguments']).data
    assert imported['sha256'] == render['output']['sha256']
    assert call(hint['tool'], hint['arguments']).data['ref'] == imported['ref']
    original = Path(ctx.ws.load().sources[0].path)
    info = probe(original, with_keyframes=False)
    assert (info.width, info.height, info.n_frames) == (64, 48, 6)
    frame = call('media_frame', {'target': 'source:' + imported['ref'], 'frame': 5})
    assert frame.images
    (ctx.ws.work / render['output']['path']).write_bytes(b'changed working copy')
    assert original.read_bytes() != b'changed working copy'
    assert settings.read_bytes() == settings_before
    assert not list(ctx.ws.canvas.glob('*/HEAD*'))
    logged = {json.loads(line)['tool'] for line in (ctx.ws.ledger / 'tools.jsonl').read_text().splitlines()}
    assert set(target['required_tools'] + target['import_tools']) <= logged
