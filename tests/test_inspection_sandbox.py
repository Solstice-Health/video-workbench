"""General image coordinates and isolated execution contracts, without model calls."""
from __future__ import annotations

import io
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from vwb.tools import inspection_tools as inspect
from vwb.tools import sandbox_tools as sandbox
from vwb.tools.base import Registry, ToolContext
from vwb.workspace.registry import Registry as Assets


@pytest.fixture
def ctx(tmp_path):
    ws = SimpleNamespace(root=tmp_path, work=tmp_path / 'work', assets=tmp_path / 'assets',
                         ledger=tmp_path / 'ledger')
    for path in (ws.work, ws.assets, ws.ledger):
        path.mkdir()
    return ToolContext(ws=ws, agent='inspection-test', role='job_lead', gateway=None)


def call(ctx, name, args):
    return Registry(inspect.TOOLS + sandbox.TOOLS).call(ctx, name, args)


def make_image(ctx, name='input.png', size=(200, 100)):
    pixels = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    pixels[:, :, 0] = np.arange(size[0], dtype=np.uint8)
    pixels[:, :, 1] = np.arange(size[1], dtype=np.uint8)[:, None]
    path = ctx.ws.work / name
    Image.fromarray(pixels).save(path)
    return path


def test_crop_zoom_maps_display_back_to_original_pixels(ctx):
    make_image(ctx)
    result = call(ctx, 'image_inspect', {'source': 'input.png', 'crop': [40, 20, 50, 40], 'zoom': 2})
    assert not result.is_error
    assert result.data['source_size'] == [200, 100]
    assert result.data['display_size'] == [100, 80]
    transform = result.data['display_to_source']
    assert transform == {'scale_x': .5, 'scale_y': .5, 'offset_x': 40, 'offset_y': 20}
    with Image.open(io.BytesIO(result.images[0])) as image:
        assert image.size == (100, 80)
        assert image.getpixel((50, 40))[:2] == (65, 40)
    assert (ctx.ws.ledger / 'tools.jsonl').exists()


def test_inspection_caps_edge_and_clips_partial_crop(ctx):
    make_image(ctx)
    result = call(ctx, 'image_inspect', {'source': 'work/input.png', 'crop': [-20, 0, 100, 80],
                                        'zoom': 8, 'max_edge': 64})
    assert result.data['crop'] == [0, 0, 80, 80]
    assert result.data['display_size'] == [64, 64]
    assert result.data['display_to_source']['scale_x'] == 1.25


def test_asset_revision_is_pinned_in_result(ctx):
    path = make_image(ctx)
    ref = Assets(ctx.ws.assets).register(path, slot='example', kind='image', receipt='human.upload', derived_from=[])
    result = call(ctx, 'image_inspect', {'source': 'slot:example'})
    assert not result.is_error
    assert result.data['source'] == ref


def test_orientation_is_applied_before_coordinate_system(ctx):
    path = make_image(ctx)
    with Image.open(path) as image:
        exif = image.getexif()
        exif[274] = 6
        image.save(ctx.ws.work / 'rotated.jpg', exif=exif)
    result = call(ctx, 'image_inspect', {'source': 'rotated.jpg'})
    assert result.data['source_size'] == [100, 200]


@pytest.mark.parametrize('args', [
    {'source': '../outside.png'}, {'source': '/etc/passwd'}, {'source': '.private/image.png'},
    {'source': 'input.png', 'crop': [0, 0, 0, 20]}, {'source': 'input.png', 'crop': [400, 0, 20, 20]},
    {'source': 'input.png', 'crop': [0, float('nan'), 20, 20]}, {'source': 'input.png', 'crop': 'bad'},
    {'source': 'input.png', 'zoom': True}, {'source': 'input.png', 'zoom': 9},
    {'source': 'input.png', 'max_edge': 1600},
])
def test_invalid_image_requests_fail(ctx, args):
    make_image(ctx)
    assert call(ctx, 'image_inspect', args).is_error


def test_symlink_file_and_parent_cannot_escape(ctx):
    outside = ctx.ws.root / 'outside'
    outside.mkdir()
    Image.new('RGB', (10, 10)).save(outside / 'image.png')
    (ctx.ws.work / 'link.png').symlink_to(outside / 'image.png')
    (ctx.ws.work / 'folder').symlink_to(outside, target_is_directory=True)
    assert call(ctx, 'image_inspect', {'source': 'link.png'}).is_error
    assert call(ctx, 'image_inspect', {'source': 'folder/image.png'}).is_error


class Process:
    def __init__(self, *, output=b'created\n', error=b'', code=0, running=False):
        self.stdout, self.stderr = io.BytesIO(output), io.BytesIO(error)
        self.returncode = None if running else code
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def runner(monkeypatch):
    observed = {'commands': [], 'removed': [], 'process': Process()}
    monkeypatch.setattr(sandbox.shutil, 'which', lambda name: '/usr/bin/docker')

    def start(command, **kwargs):
        observed['commands'].append((command, kwargs))
        return observed['process']

    monkeypatch.setattr(sandbox.subprocess, 'Popen', start)
    monkeypatch.setattr(sandbox, '_remove', lambda name: observed['removed'].append(name))
    return observed


def test_sandbox_keeps_argv_literal_and_never_passes_keys(ctx, runner, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'not-a-real-key')
    monkeypatch.setenv('VWB_UI_JOB_ID', 'a' * 32)
    command = ['python3', '-c', 'print("$(do-not-run)")']
    result = call(ctx, 'sandbox_run', {'argv': command})
    assert not result.is_error
    assert result.data['stdout'] == 'created\n'
    argv, kwargs = runner['commands'][0]
    assert argv[-len(command):] == command
    assert '--network=none' in argv and '--read-only' in argv and '--cap-drop=ALL' in argv
    assert '--security-opt=no-new-privileges' in argv and '--pull=never' in argv
    assert '--memory=512m' in argv and '--pids-limit=64' in argv
    assert 'vwb.ui-job=' + 'a' * 32 in argv
    assert argv[argv.index('--volume') + 1] == f'{ctx.ws.work}:/workspace:rw'
    assert 'not-a-real-key' not in repr(argv) and '-e' not in argv
    assert not kwargs.get('shell')
    assert runner['removed']


def test_sandbox_output_is_bounded_but_pipes_are_drained(ctx, runner):
    runner['process'] = Process(output=b'x' * 100_000, error=b'y' * 100_000)
    result = call(ctx, 'sandbox_run', {'argv': ['python3', 'script.py']})
    assert len(result.data['stdout']) == sandbox.OUTPUT_BYTES
    assert len(result.data['stderr']) == sandbox.OUTPUT_BYTES
    assert result.data['stdout_truncated'] and result.data['stderr_truncated']


def test_sandbox_failure_is_tool_error_with_structured_output(ctx, runner):
    runner['process'] = Process(error=b'bad script', code=1)
    result = call(ctx, 'sandbox_run', {'argv': ['python3', 'script.py']})
    assert result.is_error and result.data['exit_code'] == 1
    assert result.data['status'] == 'failed'
    assert result.data['stderr'] == 'bad script'


@pytest.mark.parametrize('args', [
    {'argv': 'python script.py'}, {'argv': []}, {'argv': ['bad\0arg']}, {'argv': ['x' * 32_001]},
    {'argv': ['python3'], 'timeout_s': 1801}, {'argv': ['python3'], 'timeout_s': float('inf')},
    {'argv': ['python3'], 'timeout_s': True}, {'argv': ['python3'], 'cwd': '../'},
    {'argv': ['python3'], 'cwd': '/tmp'}, {'argv': ['python3'], 'cwd': 'missing'},
])
def test_invalid_execution_does_not_launch(ctx, runner, args):
    assert call(ctx, 'sandbox_run', args).is_error
    assert runner['commands'] == []


def test_cancel_before_launch(ctx, runner):
    event = threading.Event()
    event.set()
    ctx.state['cancel_event'] = event
    assert call(ctx, 'sandbox_run', {'argv': ['python3']}).is_error
    assert not runner['commands']


def test_cancel_during_command_kills_container_and_client(ctx, runner, monkeypatch):
    runner['process'] = Process(running=True)
    event = threading.Event()
    ctx.state['cancel_event'] = event
    monkeypatch.setattr(sandbox.time, 'sleep', lambda seconds: event.set())
    result = call(ctx, 'sandbox_run', {'argv': ['python3', 'script.py']})
    assert result.is_error and result.data['status'] == 'cancelled'
    assert runner['process'].killed and runner['removed']


def test_host_deadline_kills_container(ctx, runner, monkeypatch):
    runner['process'] = Process(running=True)
    times = iter([0, 7, 7])
    monkeypatch.setattr(sandbox.time, 'monotonic', lambda: next(times))
    result = call(ctx, 'sandbox_run', {'argv': ['python3', 'script.py'], 'timeout_s': 1})
    assert result.is_error and result.data['status'] == 'timed_out'
    assert runner['process'].killed and runner['removed']


def test_no_docker_has_no_host_fallback(ctx, runner, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, 'which', lambda _: None)
    result = call(ctx, 'sandbox_run', {'argv': ['python3']})
    assert result.is_error and 'no host execution' in result.text
    assert not runner['commands']


def test_new_capabilities_can_be_disabled_before_execution(ctx, runner, monkeypatch):
    monkeypatch.setattr(Registry, 'mode', staticmethod(lambda cap: 'block'))
    assert call(ctx, 'sandbox_run', {'argv': ['python3']}).is_error
    assert call(ctx, 'image_inspect', {'source': 'input.png'}).is_error
    assert not runner['commands']
