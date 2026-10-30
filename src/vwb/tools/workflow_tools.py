"""Bounded workflow execution and source inspection through the normal Registry."""
from __future__ import annotations

import json

from vwb import workflows
from vwb.canvas.paths import checked_path, read_record
from vwb.design_context import store as evidence
from vwb.tools.base import Tool, ToolResult, schema
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json, write_json


def _owned(ctx, ident):
    row = workflows.load(ctx.ws, ident)
    if ctx.role == 'brand_context' and row['worker'] != ctx.agent:
        raise ValueError('Only the assigned specialist may inspect or submit this workflow.')
    return row


def workflow_start(ctx, args):
    from vwb.tools import build_registry
    if args:
        raise ValueError('Workflow inputs come only from this human turn, not tool arguments.')
    turns = ctx.ws.load().chat
    if not turns or turns[-1].role != 'human':
        raise ValueError('A workflow must be explicitly selected in the current human message.')
    context = turns[-1].context
    if context.get('workflows') != ['brand-context']:
        raise ValueError('Select /brand-context and tag a folder first.')
    workflows.validate_selection(ctx.ws, context)
    if ctx.swarm is None:
        raise ValueError('Workflow execution requires a live agent tree.')
    mapping_path = ctx.ws.memory / 'workflow_turns.json'
    mapping = read_json(mapping_path, {})
    key = (sha256_json({'origin': ctx.state['workflow_origin'], 'project': ctx.ws.project_root.name})
           if ctx.state.get('workflow_origin') else sha256_json(turns[-1].model_dump(mode='json')))
    ident = mapping.get(key)
    row = (workflows.load(ctx.ws, ident) if ident else
           workflows.prepare(ctx.ws, context, turn_id=ctx.state.get('workflow_turn_id', key),
                             human_request=ctx.state.get('workflow_brief') or turns[-1].text))
    mapping[key] = row['id']
    # Lead tool calls later in this turn use its persisted ChatTurn identity.
    mapping[sha256_json(turns[-1].model_dump(mode='json'))] = row['id']
    write_json(mapping_path, mapping)
    write_json(ctx.ws.memory / 'workflow_active.json', {'id': row['id']})
    if row['status'] in {'published', 'candidate_ready'} or row['worker'] in ctx.swarm.tickets:
        return ToolResult.json(workflows.read(ctx.ws, row['id']))
    if row['status'] == 'failed':
        raise ValueError('Source snapshot failed. Correct the inputs and submit a new workflow.')
    def assign(ticket):
        row.update(worker=ticket, status='running')
        workflows.save(ctx.ws, row)
    ctx.state['agent_created_callback'] = assign
    try:
        spawned = build_registry().call(ctx, 'agent_spawn', {
            'role': 'brand_context', 'task': f"Build the brand context candidate for workflow {row['id']}. "
            "Read workflow_read for the initiating human brief, schemas and every page of the inventory and skips. "
            "Follow next_brief_offset too; its full brief is authoritative over the old conversation request. Inspect source evidence with "
            "workflow_source, then submit the structured candidate using workflow_draft. Account for every file. "
            "Do not publish or claim independent approval. Report gaps and conflicting evidence to the lead.",
            'context': {'workflow_id': row['id'], 'project_id': row['project_id'],
                        'human_request': row['human_request'][:16000],
                        'more_brief': len(row['human_request']) > 16000}})
    finally:
        ctx.state.pop('agent_created_callback', None)
    if spawned.is_error or not spawned.data or spawned.data.get('parked'):
        return spawned
    ctx.ledger.append('workflow.started', ctx.agent, {'id': row['id'], 'worker': row['worker'], 'sources': len(row['sources'])})
    return ToolResult.json(workflows.read(ctx.ws, row['id']))


def workflow_read(ctx, args):
    _owned(ctx, args['workflow_id'])
    result = workflows.read(ctx.ws, args['workflow_id'], offset=args.get('offset', 0),
                            skipped_offset=args.get('skipped_offset', 0), brief_offset=args.get('brief_offset', 0))
    if args.get('candidate'):
        result['candidate'] = read_record(workflows._root(ctx.ws), workflows._id(args['workflow_id']) + '-candidate.json')
    return ToolResult.json(result)


def workflow_source(ctx, args):
    row = _owned(ctx, args['workflow_id'])
    source = next((s for s in row['sources'] if s['id'] == args['source']), None)
    if source is None:
        raise ValueError('Choose a source ID from this workflow inventory.')
    action, options = args.get('action', 'read'), args.get('options', {})
    if not isinstance(options, dict):
        raise ValueError('Source options must be an object.')
    metadata = {k: source[k] for k in ('id', 'path', 'sha256', 'bytes', 'inventory_pack')}
    if action == 'read':
        offset = options.get('offset', 0)
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be a nonnegative character index.')
        if 'text_file' not in source:
            return ToolResult.json({**metadata, 'notice': 'Binary source: use a PDF, image or video action. Unsupported formats must be reported as gaps.', 'trust': workflows.TRUST})
        text = evidence.source_bytes(ctx.ws, source['inventory_pack'], source, text=True).decode()
        return ToolResult.json({**metadata, 'text': text[offset:offset + 16000], 'offset': offset,
                                'next_offset': offset + 16000 if offset + 16000 < len(text) else None, 'trust': workflows.TRUST})
    path = evidence.materialize(ctx.ws, source['inventory_pack'], source)
    if action in {'pdf_info', 'pdf_read', 'pdf_render', 'pdf_search', 'image_inspect'}:
        from jsonschema import validate

        from vwb.tools import inspection_tools, pdf_tools
        tool = next(t for t in [*pdf_tools.TOOLS, *inspection_tools.TOOLS] if t.name == action)
        # Reuse bounded parsers as implementations, without exposing arbitrary
        # paths or a general file/network tool to the specialist.
        actual = {**options, 'source': path}
        validate(actual, tool.schema)
        result = tool.handler(ctx, actual)
        result.text = json.dumps(metadata) + '\n' + result.text
        return result
    if action == 'video_frames':
        from vwb.media.decode import read_frames
        from vwb.media.frames import jpeg_bytes, resize_to_width
        from vwb.media.probe import probe
        actual = checked_path(ctx.ws.work, *path.split('/'))
        info = probe(actual, with_keyframes=False)
        times = options.get('times', [0])
        if not isinstance(times, list) or not 1 <= len(times) <= 4 or any(type(t) not in (int, float) or not 0 <= t < info.duration for t in times):
            raise ValueError('Choose 1–4 times within this video duration.')
        if info.width * info.height > 40_000_000 or not info.fps:
            raise ValueError('Video dimensions or frame rate exceed inspection limits.')
        numbers = [int(t * info.fps) for t in times]
        frames = read_frames(actual, numbers)
        return ToolResult.json({**metadata, 'times': times, 'duration_s': info.duration,
                                'notice': 'Sampled frames only; audio and other times have not been inspected.'},
                               images=[jpeg_bytes(resize_to_width(frames[n], min(1280, info.width))) for n in numbers])
    raise ValueError('Unknown source inspection action.')


def workflow_draft(ctx, args):
    row = _owned(ctx, args['workflow_id'])
    if ctx.role != 'job_lead' and ctx.agent != row['worker']:
        raise ValueError('Only the assigned specialist or lead may submit this candidate.')
    return ToolResult.json(workflows.write_candidate(ctx.ws, args['workflow_id'], args['candidate']))


def workflow_publish(ctx, args):
    row = _owned(ctx, args['workflow_id'])
    if ctx.swarm and row['worker'] in ctx.swarm.tickets and ctx.swarm.tickets[row['worker']]['status'] not in {'done', 'failed', 'interrupted'}:
        raise ValueError('Wait for the specialist to finish before reviewing and publishing its candidate.')
    result = workflows.publish(ctx.ws, row['id'])
    ctx.ledger.append('workflow.published', ctx.agent, {'workflow': row['id'], 'key': result['key'], 'revision': result['revision']})
    return ToolResult.json(result, artifacts=[result['revision']])


ID = {'type': 'string', 'pattern': '^wf-[a-f0-9]{32}$'}
TOOLS = [
    Tool('workflow_start', 'Start the workflow explicitly selected in the current human message. Snapshots tagged folders and delegates to a restricted specialist. Idempotent within this human turn.', schema({}), workflow_start, 'workflow.execute', 'workflows'),
    Tool('workflow_read', 'Read workflow status, paged human brief, pinned source inventory and skipped paths, candidate schemas, or the submitted candidate. Follow every next_*_offset.', schema({'workflow_id': ID, 'offset': {'type': 'integer', 'minimum': 0}, 'skipped_offset': {'type': 'integer', 'minimum': 0}, 'brief_offset': {'type': 'integer', 'minimum': 0}, 'candidate': {'type': 'boolean'}}, ['workflow_id']), workflow_read, 'workflow.read', 'workflows'),
    Tool('workflow_source', 'Inspect only a pinned source in your assigned workflow. Text pages, bounded PDF read/render/search, image inspection, or 1–4 sampled video frames; no network or document execution.', schema({'workflow_id': ID, 'source': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['read', 'pdf_info', 'pdf_read', 'pdf_search', 'pdf_render', 'image_inspect', 'video_frames']}, 'options': {'type': 'object'}}, ['workflow_id', 'source']), workflow_source, 'workflow.read', 'workflows'),
    Tool('workflow_draft', 'Submit a source-cited brand pack, memory and excluded-file report as a candidate. This does not publish shared memory.', schema({'workflow_id': ID, 'candidate': {'type': 'object'}}, ['workflow_id', 'candidate']), workflow_draft, 'workflow.draft', 'workflows'),
    Tool('workflow_publish', 'After inspecting the specialist candidate and source evidence, validate coverage and publish an immutable project-memory revision. Required rules need citations. Does not bind outputs or certify approvals.', schema({'workflow_id': ID}, ['workflow_id']), workflow_publish, 'project_memory.write', 'workflows'),
]
