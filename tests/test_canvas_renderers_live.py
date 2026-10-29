"""Opt-in end-to-end tests: VWB_RENDER_SMOKE=1 pytest tests/test_canvas_renderers_live.py."""
import os

import numpy as np
import pytest
from PIL import Image

from vwb.canvas.compile import load_working, plan
from vwb.canvas.store import CanvasStore
from vwb.media.probe import probe
from vwb.render import pipeline
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.tools.canvas_tools import _render_frames
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry
from vwb.workspace.request import SUBDIRS, Request

pytestmark = pytest.mark.skipif(os.environ.get('VWB_RENDER_SMOKE') != '1', reason='Opt-in local renderer integration')


@pytest.mark.parametrize('renderer', ['remotion', 'browser', 'gsap', 'canvas2d'])
def test_registry_preview_commit_render_and_swap(tmp_path, renderer):
    ws = Workspace(tmp_path / 'r-renderers')
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message='Test adapter rendering'))
    ctx = ToolContext(ws, 'job_lead', 'job_lead', None)
    tools = build_registry()
    made = tools.call(ctx, 'canvas_new', {'output': 'card', 'duration_s': 0.2, 'renderer': renderer})
    assert not made.is_error, made.text
    path = ws.work / made.data['path']
    doc = read_json(path)
    doc['format'].update(width=160, height=96)
    doc['layers'] = [
        {'id': 'background', 'type': 'shape', 'shape': 'rect', 'fill': '#102030', 'box': [0, 0, 160, 96]},
        {'id': 'moving', 'type': 'shape', 'shape': 'circle', 'fill': '#44CCFF', 'span': [2, 6],
         'box': [{'f': 2, 'v': [5, 10, 30, 30]}, {'f': 5, 'v': [110, 10, 30, 30], 'ease': 'linear'}]},
    ]
    if renderer != 'canvas2d':
        added = tools.call(ctx, 'copy_add', {'id': 'title', 'source': 'authored', 'text': 'Editable title', 'note': 'Test'})
        assert not added.is_error, added.text
        doc['layers'].append({'id': 'title', 'type': 'text', 'copy_id': 'title', 'box': [4, 58, 150, 30],
                              'style': {'font': 'Inter', 'weight': 600, 'size': 18, 'color': '#FFFFFF'}})
    write_json(path, doc)
    checked = tools.call(ctx, 'canvas_check', {'output': 'card'})
    assert not checked.is_error and not checked.data['errors'], checked.text
    preview = tools.call(ctx, 'canvas_preview', {'output': 'card', 'times': [0, 0.1, 0.166], 'guides': False})
    assert not preview.is_error and preview.images, preview.text
    committed = tools.call(ctx, 'canvas_commit', {'output': 'card'})
    assert not committed.is_error, committed.text
    canvas = CanvasStore(ws.canvas).get('card')
    first = _render_frames(ctx, canvas, [0, 3, 5])
    repeated = _render_frames(ctx, canvas, [5, 0, 3])
    assert all(np.array_equal(first[f], repeated[f]) for f in first)
    assert not np.array_equal(first[0], first[5])
    # Inspect a known opaque shape color and transparent plate/background behavior.
    assert np.max(np.abs(first[0][90, 159].astype(int) - [16, 32, 48])) <= 1
    receipt = pipeline.render(ws, 'card')
    info = probe(receipt['file'], with_keyframes=False)
    assert (info.n_frames, info.width, info.height) == (6, 160, 96)
    assert receipt['tools']['renderer'] == renderer
    assert receipt['tools']['generator'] == canvas.renderer_pin
    assert receipt['inputs']['plates']
    assert pipeline.render(ws, 'card') == receipt
    original = CanvasStore(ws.canvas).path('card', 1).read_bytes()
    alternate = 'browser' if renderer != 'browser' else 'gsap'
    swapped = tools.call(ctx, 'canvas_set_renderer', {'output': 'card', 'renderer': alternate})
    assert not swapped.is_error, swapped.text
    newer, _ = CanvasStore(ws.canvas).commit(ws, 'card', 'test')
    assert newer.version == 2 and newer.parent == 1 and newer.layers == canvas.layers
    assert plan(newer, Registry(ws.assets))[0].plate_id != plan(canvas, Registry(ws.assets))[0].plate_id
    assert CanvasStore(ws.canvas).path('card', 1).read_bytes() == original
    assert load_working(ws, 'card').renderer == alternate
    # Artifact for local inspection when --basetemp is supplied.
    Image.fromarray(first[5]).save(tmp_path / f'{renderer}-frame5.png')
