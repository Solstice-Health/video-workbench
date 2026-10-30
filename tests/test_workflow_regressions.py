"""Regression cases from the independent workflow review; synthetic inputs only."""
import pytest
from test_brand_workflow import candidate, workspace
from test_brand_workflow import ws as brand_workspace

from vwb import workflows
from vwb.agents import swarm as swarm_module
from vwb.design_context import store as evidence
from vwb.project_memory import store as memory
from vwb.tools import build_registry, workflow_tools
from vwb.tools.base import ToolContext
from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import TurnQueue
from vwb.workspace.request import ChatTurn

ws = brand_workspace


def test_qc_lane_cannot_start_brand_memory_workflow(ws):
    from vwb.qc.chat import QCWorkspace
    with pytest.raises(ValueError, match='Edit conversation'):
        workflows.validate_selection(QCWorkspace(ws), {'workflows': ['brand-context'], 'folders': ['.']})


def test_publication_never_reads_agent_mutable_manifests_or_inspection_copies(ws, monkeypatch):
    row = workflows.prepare(ws, ws.load().chat[-1].context, turn_id=1)
    source = row['sources'][0]
    path = evidence.materialize(ws, source['inventory_pack'], source)
    (ws.work / path).write_text('Forged replacement source')
    workflows.write_candidate(ws, row['id'], candidate(row))
    original_export = workflows._export
    def corrupt_export(workspace, ident, name, value):
        original_export(workspace, ident, name, value)
        if name == 'pack.json':
            (workspace.work / row['output_path'] / name).write_text('{"brand":"Forged"}')
    monkeypatch.setattr(workflows, '_export', corrupt_export)
    published = workflows.publish(ws, row['id'])
    pack = evidence.load(ws, published['pack'])
    assert pack['brand'] == 'Synthetic'
    assert pack['sources'][0]['sha256'] == source['sha256']
    assert b'Forged' not in evidence.source_bytes(ws, published['pack'], pack['sources'][0])


def test_mutated_pinned_bytes_fail_before_new_pack_is_published(ws):
    row = workflows.prepare(ws, ws.load().chat[-1].context, turn_id=1)
    workflows.write_candidate(ws, row['id'], candidate(row))
    source = row['sources'][0]
    before = evidence.catalog(ws)['total']
    pinned = ws.project_root / '.design-context' / source['inventory_pack'] / source['file']
    pinned.write_text('Corrupt pinned bytes')
    with pytest.raises(ValueError, match='integrity'):
        workflows.publish(ws, row['id'])
    assert evidence.catalog(ws)['total'] == before
    assert not memory.heads(ws)


def test_resume_before_mapping_does_not_reuse_old_completed_workflow(ws, monkeypatch):
    old = workflows.prepare(ws, ws.load().chat[-1].context, turn_id=1)
    workflows.write_candidate(ws, old['id'], candidate(old))
    workflows.publish(ws, old['id'])
    write_json(ws.memory / 'workflow_active.json', {'id': old['id']})
    (ws.work / 'new-brand').mkdir()
    (ws.work / 'new-brand/new.md').write_text('A different brand')
    req = ws.load()
    req.chat.append(ChatTurn(role='human', text='Resume interrupted turn',
                            context={'workflows': ['brand-context'], 'folders': ['new-brand']}))
    ws.save(req)
    received = []
    monkeypatch.setattr(swarm_module, 'run_worker', lambda *args, **kw: received.append(args[5]))
    registry = build_registry()
    tree = swarm_module.Swarm(ws, registry)
    ctx = ToolContext(ws, 'job_lead', 'job_lead', None, tree)
    ctx.state.update(workflow_origin='b' * 32, workflow_brief='/brand-context for UK clinicians only')
    try:
        result = workflow_tools.workflow_start(ctx, {})
        tree.wait([], 5, actor='job_lead', mode='all')
        assert result.data['id'] != old['id']
        assert result.data['published'] is None
        assert received[0]['human_request'] == '/brand-context for UK clinicians only'
        assert workflows.load(ws, result.data['id'])['folders'] == ['new-brand']
    finally:
        tree.shutdown()


def test_unassigned_general_worker_cannot_replace_candidate(ws):
    row = workflows.prepare(ws, ws.load().chat[-1].context, turn_id=1)
    row['worker'] = 'specialist'
    workflows.save(ws, row)
    ctx = ToolContext(ws, 'unrelated', 'worker', None)
    args = {'workflow_id': row['id'], 'candidate': candidate(row)}
    assert build_registry().call(ctx, 'workflow_draft', args).is_error
    with pytest.raises(ValueError, match='assigned'):
        workflow_tools.workflow_draft(ctx, args)
    assert workflows.load(ws, row['id'])['status'] != 'candidate_ready'


def test_shared_memory_retains_exclusions_and_skips_even_if_model_gaps_are_empty(ws):
    (ws.work / 'brand/unused.txt').write_text('Reference not inspected')
    (ws.work / 'brand/.private').write_text('Hidden input')
    row = workflows.prepare(ws, ws.load().chat[-1].context, turn_id=1)
    draft = candidate(row)
    draft['memory']['gaps'] = []
    draft['excluded'] = [{'path': 'brand/unused.txt', 'reason': 'Not inspected'}]
    workflows.write_candidate(ws, row['id'], draft)
    assert workflows.read(ws, row['id'])['skipped'][0]['path'] == 'brand/.private'
    result = workflows.publish(ws, row['id'])
    peer = workspace(ws.root.parent, 'r-peer')
    from vwb.workspace.projects import ProjectStore
    ProjectStore(ws.root.parent).move(peer, ws.project_root.name)
    coverage = memory.read(peer, result['key'])['coverage']
    assert coverage['excluded'] == draft['excluded']
    assert coverage['skipped'][0]['path'] == 'brand/.private'
    assert memory.search(peer)['hits'][0]['coverage'] == {'excluded': 1, 'skipped': 1}


def test_root_reingestion_does_not_ingest_its_own_generated_copies(ws):
    row = workflows.prepare(ws, {'folders': ['.'], 'workflows': ['brand-context']}, turn_id=1)
    source = row['sources'][0]
    evidence.materialize(ws, source['inventory_pack'], source)
    sources, skipped = workflows.inventory(ws, ['.'])
    assert [s['path'] for s in sources] == ['brand/guide.md']
    assert {s['path'] for s in skipped} >= {'design-context', 'brand-context/workflows'}


def test_snapshot_actual_bytes_rechecked_after_inventory(ws, monkeypatch):
    original = workflows.inventory
    def grow_after_scan(*args):
        result = original(*args)
        (ws.work / 'brand/guide.md').write_text('x' * 201)
        return result
    monkeypatch.setattr(workflows, 'MAX_BYTES', 200)
    monkeypatch.setattr(workflows, 'inventory', grow_after_scan)
    with pytest.raises(ValueError, match='Snapshot exceeds'):
        workflows.prepare(ws, ws.load().chat[-1].context, turn_id=1)
    assert not memory.heads(ws)


def test_queue_slash_selection_and_resume_preserve_exact_origin(ws, monkeypatch):
    monkeypatch.setattr(TurnQueue, '_dispatch', lambda self: None)
    queue = TurnQueue(ws.root.parent, command=['unused-worker'])
    try:
        job = queue.enqueue(ws, message='  /brand-context for UK clinicians', context={'folders': ['brand']})
        assert job['context']['workflows'] == ['brand-context']
        queue.states[ws.id]['jobs'][0]['status'] = 'stopped'
        queue._save(ws)
        queue.resume(ws, job_id=job['id'])
        resumed = read_json(ws.memory / 'web_queue.json')['jobs'][-1]
        assert resumed['workflow_origin'] == job['id']
        assert resumed['resume_message'] == '/brand-context for UK clinicians'
        queue.states[ws.id]['jobs'][-1]['status'] = 'stopped'
        queue._save(ws)
        queue.resume(ws, job_id=resumed['id'])
        assert read_json(ws.memory / 'web_queue.json')['jobs'][-1]['workflow_origin'] == job['id']
    finally:
        queue.close()
