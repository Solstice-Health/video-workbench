"""Offline video inputs and inspection receipts, independent of edit indexes."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from vwb.index.paths import index_dir
from vwb.index.store import IndexStore
from vwb.media import inputs
from vwb.tools import index_tools, media_tools
from vwb.tools.base import ToolContext
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace, attachments
from vwb.workspace.media import register_work_file
from vwb.workspace.registry import Registry
from vwb.workspace.request import SUBDIRS, Request

VIDEO = b'\x00\x00\x00\x18ftypisom' + b'synthetic-video'


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / 'r-inspection')
    for name in SUBDIRS:
        (ws.root / name).mkdir(parents=True)
    ws.save(Request(id=ws.id, message='Inspect the selected media'))
    metadata = Mock(return_value=SimpleNamespace(fps=30, duration=2.0, n_frames=60, width=64, height=48))
    monkeypatch.setattr(inputs, 'video_stream_info', metadata)
    monkeypatch.setattr(media_tools, 'video_stream_info', metadata)
    monkeypatch.setattr(media_tools, 'read_frames', lambda path, frames, **kwargs:
                        {f: np.zeros((48, 64, 3), dtype=np.uint8) for f in frames})
    return ToolContext(ws, 'lead', 'job_lead', None)


@pytest.fixture
def provider(monkeypatch):
    upload = Mock(return_value={'uri': 'fake'})
    generate = Mock(return_value=SimpleNamespace(text='An offline answer', model='fixture-video-model'))
    monkeypatch.setattr(media_tools.gem, 'upload', upload)
    monkeypatch.setattr(media_tools.gem, 'video_part', lambda *args, **kwargs: {'video': kwargs})
    monkeypatch.setattr(media_tools.gem, 'generate', generate)
    return SimpleNamespace(upload=upload, generate=generate)


def work_video(ctx, name='clip.mp4', data=VIDEO):
    path = ctx.ws.work / name
    path.write_bytes(data)
    return path


def source_video(ctx, name='clip.mp4', data=VIDEO):
    return register_work_file(ctx.ws, work_video(ctx, name, data).name)


def render(ctx, version=1):
    folder = ctx.ws.canvas / 'design'
    folder.mkdir(exist_ok=True)
    write_json(folder / f'v{version}.json', {'schema': 'vwb.canvas/2', 'profile': 'youtube_shorts',
               'request': ctx.ws.id, 'output': 'design',
               'format': {'width': 64, 'height': 48, 'fps': 30}, 'version': version,
               'time_map': [], 'duration_frames': 60, 'layers': []})
    (folder / 'HEAD').write_text(str(version))
    target = ctx.ws.renders / 'design' / f'v{version}' / f'design_v{version}.mp4'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(VIDEO + str(version).encode())
    return target


def populate_index(ctx, src):
    path = index_dir(ctx.ws, src.ref) / 'index.sqlite'
    st = IndexStore(path)
    st.set_media(src.media_id, src.rev, src.path, {'fps': 30, 'width': 64, 'height': 48,
                                                'n_frames': 60, 'duration': 2})
    st.connect().close()
    return path


def test_watch_source_free_output_is_persisted_before_provider_and_pins_version(ctx, provider):
    path = render(ctx)

    def generate(*args, **kwargs):
        records = list((ctx.ws.memory / 'media_observations').glob('*.json'))
        assert len(records) == 1 and read_json(records[0])['status'] == 'pending'
        render(ctx, 2)  # another version becoming HEAD must not change attribution
        return SimpleNamespace(text='Rendered version one', model='fixture')

    provider.generate.side_effect = generate
    result = media_tools.watch_ask(ctx, {'target': 'output:design', 'question': 'Read this design'})
    receipt = read_json(ctx.ws.root / result.data['receipt'])
    assert not ctx.ws.load().sources
    assert receipt['target'] == result.data['target'] == 'output:design@v1'
    assert receipt['identity'] == {'kind': 'output', 'ref': 'output:design@v1', 'output': 'design',
                                   'version': 1, 'target': 'output:design@v1', 'sha256': sha256_file(path)}
    assert receipt['status'] == 'claim' and receipt['answer'] == 'Rendered version one'
    assert receipt['f0'] == 0 and receipt['f1'] == 60
    assert provider.upload.call_args.args[1] == path
    assert list(ctx.ws.index.iterdir()) == []


def test_watch_second_source_identity_never_uses_first_index(ctx, provider):
    first = source_video(ctx, 'one.mp4', VIDEO + b'first')
    second = source_video(ctx, 'two.mp4', VIDEO + b'second')
    original = populate_index(ctx, first)
    before = original.read_bytes()
    result = media_tools.watch_ask(ctx, {'target': f'source:{second.ref}', 'question': 'Which source?',
                                       't0': 0.25, 't1': 1.0})
    receipt = read_json(ctx.ws.root / result.data['receipt'])
    assert receipt['target'] == f'source:{second.ref}'
    assert receipt['identity']['media'] == second.ref
    assert receipt['identity']['sha256'] == second.sha256
    assert provider.upload.call_args.args[1] == Path(second.path)
    assert original.read_bytes() == before
    assert not index_dir(ctx.ws, second.ref).exists()


@pytest.mark.parametrize('args', [
    {'t0': -1}, {'t1': float('nan')}, {'t1': float('inf')}, {'t0': 1, 't1': 0.5},
    {'t1': 3}, {'fps': 0}, {'fps': True}, {'resolution': 'unknown'}, {'question': '  '},
    {'target': 'output:missing'}, {'target': 'output:design@v9'},
])
def test_watch_invalid_inputs_fail_before_upload_or_generation(ctx, provider, args):
    render(ctx)
    with pytest.raises((ValueError, KeyError, FileNotFoundError)):
        media_tools.watch_ask(ctx, {'target': 'output:design', 'question': 'Look', **args})
    provider.upload.assert_not_called()
    provider.generate.assert_not_called()
    assert not (ctx.ws.memory / 'media_observations').exists()


def test_watch_unwritable_receipt_destination_fails_before_provider(ctx, provider):
    render(ctx)
    (ctx.ws.memory / 'media_observations').write_text('not a directory')
    with pytest.raises(OSError):
        media_tools.watch_ask(ctx, {'target': 'output:design', 'question': 'Look'})
    provider.upload.assert_not_called()
    provider.generate.assert_not_called()


def test_watch_failure_retains_failed_receipt(ctx, provider):
    render(ctx)
    provider.generate.side_effect = RuntimeError('offline provider failure')
    with pytest.raises(RuntimeError, match='offline provider failure'):
        media_tools.watch_ask(ctx, {'target': 'output:design', 'question': 'Look'})
    records = list((ctx.ws.memory / 'media_observations').glob('*.json'))
    assert len(records) == 1 and read_json(records[0])['status'] == 'failed'
    assert list(ctx.ws.index.iterdir()) == []


@pytest.mark.parametrize('name,args', [('index_query', {'sql': 'SELECT * FROM media'}),
                                      ('index_search', {'text': 'caption'}),
                                      ('index_transcript', {}), ('index_shots', {})])
def test_index_read_without_index_does_not_create_files(ctx, name, args):
    source_video(ctx, 'first.mp4', VIDEO + b'first')
    src = source_video(ctx, 'second.mp4')
    with pytest.raises(ValueError, match='No populated index'):
        getattr(index_tools, name)(ctx, {'media_id': src.ref, **args})
    assert list(ctx.ws.index.iterdir()) == []
    assert media_tools.media_frame(ctx, {'target': f'source:{src.ref}', 'frame': 0}).images
    assert list(ctx.ws.index.iterdir()) == []


def test_empty_index_does_not_break_lightweight_frames_or_gain_a_schema(ctx):
    src = source_video(ctx)
    db = ctx.ws.index / 'index.sqlite'
    db.touch()
    assert media_tools.media_frame(ctx, {'target': f'source:{src.ref}', 'frame': 0}).images
    assert media_tools.media_contact_sheet(ctx, {'target': f'source:{src.ref}', 't1': 1}).images
    with pytest.raises(ValueError, match='No populated index'):
        index_tools.index_search(ctx, {'text': 'caption'})
    assert db.read_bytes() == b''


def test_overlay_without_index_does_not_create_it(ctx):
    src = source_video(ctx)
    with pytest.raises(ValueError, match='No populated index'):
        media_tools.media_frame(ctx, {'target': f'source:{src.ref}', 'show': ['text']})
    assert list(ctx.ws.index.iterdir()) == []


def test_index_reads_use_readonly_connection_and_do_not_migrate_schema(ctx):
    src = source_video(ctx)
    path = ctx.ws.index / 'index.sqlite'
    # An older schema without text3 must stay unchanged during a read.
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE media (media_id TEXT, rev TEXT, fps REAL)')
        con.execute('INSERT INTO media VALUES (?, ?, 30)', (src.media_id, src.rev))
        con.execute('CREATE TABLE text_tracks (id INTEGER)')
    before = path.read_bytes()
    result = index_tools.index_query(ctx, {'sql': 'SELECT media_id, rev FROM media'})
    assert result.data['rows'] == [{'media_id': src.media_id, 'rev': src.rev}]
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        index_tools.index_query(ctx, {'sql': 'DELETE FROM media'})
    assert path.read_bytes() == before
    with sqlite3.connect(path) as con:
        assert [row[1] for row in con.execute('PRAGMA table_info(text_tracks)')] == ['id']


def test_index_identity_mismatch_is_rejected(ctx):
    src = source_video(ctx)
    path = populate_index(ctx, src)
    with sqlite3.connect(path) as con:
        con.execute("UPDATE media SET rev='wrong'")
    with pytest.raises(ValueError, match='identity does not match'):
        index_tools.index_query(ctx, {'sql': 'SELECT * FROM media'})


@pytest.mark.parametrize('prefix', ['', 'work:', 'work/'])
def test_work_video_is_automatically_snapshotted_without_index(ctx, prefix):
    working = work_video(ctx)
    resolved = inputs.resolve_video(ctx.ws, prefix + working.name)
    assert resolved.ref.startswith('source:src1@')
    assert resolved.lineage['kind'] == 'work_file'
    assert resolved.lineage['sha256'] == sha256_file(resolved.path)
    working.write_bytes(VIDEO + b'edited')
    assert resolved.path.read_bytes() == VIDEO
    assert list(ctx.ws.index.iterdir()) == []
    assert media_tools.media_frame(ctx, {'target': prefix + working.name, 'frame': 0}).images
    assert media_tools.media_contact_sheet(ctx, {'target': prefix + working.name, 't1': 1}).images
    assert len(ctx.ws.load().sources) == 2  # repeats deduplicate by exact content


def test_attachment_video_uses_original_ignoring_working_copy_and_source_alias(ctx):
    row = attachments.ingest(ctx.ws, [work_video(ctx)])[0]
    (ctx.ws.work / row['path']).write_bytes(VIDEO + b'changed-work-copy')
    alias = ctx.ws.root / '.attachments' / row['id'] / 'source'
    alias.symlink_to(ctx.ws.work, target_is_directory=True)
    resolved = inputs.resolve_video(ctx.ws, 'attachment:' + row['id'])
    assert resolved.path.read_bytes() == VIDEO
    assert resolved.ref.startswith('source:')
    assert resolved.lineage['ref'] == 'attachment:' + row['id']
    assert resolved.lineage['sha256'] == row['sha256']
    assert media_tools.media_frame(ctx, {'target': 'attachment:' + row['id'], 'frame': 0}).images
    assert len(ctx.ws.load().sources) == 1
    assert list(ctx.ws.index.iterdir()) == []


def test_attachment_original_tampering_rejected(ctx):
    row = attachments.ingest(ctx.ws, [work_video(ctx)])[0]
    original = ctx.ws.root / '.attachments' / row['id'] / 'original'
    original.chmod(0o644)
    original.write_bytes(VIDEO + b'tampered')
    with pytest.raises(ValueError, match='integrity'):
        inputs.resolve_video(ctx.ws, 'attachment:' + row['id'])
    assert not ctx.ws.load().sources


@pytest.mark.parametrize('path', ['../request.json', '/tmp/other.mp4', 'link.mp4'])
def test_video_input_rejects_path_escape_and_symlinks(ctx, path):
    (ctx.ws.work / 'link.mp4').symlink_to(work_video(ctx))
    with pytest.raises((ValueError, OSError, PermissionError)):
        inputs.resolve_video(ctx.ws, path)
    assert not ctx.ws.load().sources


def test_slot_video_resolves_pinned_revision_and_rejects_image(ctx):
    path = work_video(ctx)
    reg = Registry(ctx.ws.assets)
    ref = reg.register(path, slot='motion', kind='video', receipt='human.upload', derived_from=[])
    resolved = inputs.resolve_video(ctx.ws, 'slot:motion')
    assert resolved.ref == ref and resolved.lineage['ref'] == ref
    assert resolved.lineage['prov'] == 'master'
    image = reg.register(path, slot='still', kind='image', receipt='human.upload', derived_from=[])
    with pytest.raises(ValueError, match='not a video'):
        inputs.resolve_video(ctx.ws, image)


def test_source_video_does_not_hash_or_register_again(ctx, monkeypatch):
    src = source_video(ctx)
    fail = Mock(side_effect=AssertionError('unnecessary snapshot'))
    monkeypatch.setattr(inputs, 'register_work_file', fail)
    monkeypatch.setattr(inputs, '_snapshot', fail)
    monkeypatch.setattr(attachments, 'original_path', fail)
    first = inputs.resolve_video(ctx.ws, f'source:{src.ref}')
    second = inputs.resolve_video(ctx.ws, f'source:{src.ref}')
    assert first == second and first.path == Path(src.path)
    fail.assert_not_called()


def test_invalid_video_metadata_stops_before_paid_call(ctx, provider, monkeypatch):
    path = work_video(ctx)
    monkeypatch.setattr(inputs, 'video_stream_info', Mock(side_effect=ValueError('No video stream')))
    with pytest.raises(ValueError, match='No video stream'):
        media_tools.watch_ask(ctx, {'target': path.name, 'question': 'Look'})
    provider.upload.assert_not_called()
    provider.generate.assert_not_called()


def test_watch_direct_attachment_retains_original_identity(ctx, provider):
    row = attachments.ingest(ctx.ws, [work_video(ctx)])[0]
    result = media_tools.watch_ask(ctx, {'target': 'attachment:' + row['id'], 'question': 'Look'})
    saved = read_json(ctx.ws.root / result.data['receipt'])
    assert saved['identity']['ref'] == 'attachment:' + row['id']
    assert saved['identity']['sha256'] == row['sha256']
    assert saved['target'].startswith('source:src1@')
    assert list(ctx.ws.index.iterdir()) == []


def test_completed_watch_receipt_is_discoverable_and_readable_with_file_tools(ctx, provider):
    from vwb.tools.file_tools import file_list, file_read
    render(ctx)
    result = media_tools.watch_ask(ctx, {'target': 'output:design', 'question': 'Look'})
    path = result.data['path']
    assert path.startswith('inspection/watch/')
    assert path in file_list(ctx, {'glob': 'inspection/watch/*.json'}).text
    assert 'An offline answer' in file_read(ctx, {'path': path}).text
    private = read_json(ctx.ws.root / result.data['receipt'])
    assert read_json(ctx.ws.work / path) == private
    assert private['request'] == ctx.ws.id
    assert sha256_file(ctx.ws.work / path) == result.data['receipt_sha256']
    assert not result.data['warnings']


def test_unsafe_public_receipt_directory_does_not_lose_answer_or_private_receipt(ctx, provider):
    render(ctx)
    (ctx.ws.work / 'inspection').symlink_to(ctx.ws.media, target_is_directory=True)
    result = media_tools.watch_ask(ctx, {'target': 'output:design', 'question': 'Look'})
    assert result.data['answer'] == 'An offline answer' and not result.is_error
    assert result.data['path'] is None and result.data['warnings']
    assert read_json(ctx.ws.root / result.data['receipt'])['status'] == 'claim'
    assert list(ctx.ws.media.iterdir()) == []
    assert provider.generate.call_count == 1
