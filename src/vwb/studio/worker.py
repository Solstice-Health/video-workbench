"""One studio job per process. All paid generation crosses Registry.call."""
from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from vwb.gateway.core import Gateway
from vwb.studio.assets import directory, media_info, publish
from vwb.studio.models import StudioGateway
from vwb.tools import ToolContext, build_registry
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import canonical_json, read_json, write_json
from vwb.web.workspace_files import open_file
from vwb.workspace import Workspace
from vwb.workspace.ledger import Ledger
from vwb.workspace.registry import Registry
from vwb.workspace.request import now_iso


def tool_call(row: dict) -> tuple[str, dict]:
    spec, prepared = row['input'], row['prepared']
    refs = [item['ref'] for item in prepared['inputs']]
    args = {'slot': f"studio-{row['id']}", 'prompt': spec['prompt']}
    if spec['kind'] == 'image':
        args['quality'] = spec['quality']
        operation = spec['operation']
        if operation in ('create', 'edit'):
            args.update(refs=refs, size=spec['effective_size'], transparent=spec['transparent'], allow_text=spec['allow_text'])
            return 'sunburst_create', args
        args['source'] = refs[0]
        if operation == 'remove_background':
            args.update(subject=spec['subject'], method=spec.get('background_method', 'sunburst'))
            if args['method'] == 'matte':
                args['preserve_rgb'] = True
            if spec['box']:
                args['box'] = spec['box']
            return 'sunburst_extract', args
        if operation == 'inpaint':
            args['mask'] = {'asset': prepared['mask']['ref']}
            args['exact_mask'] = True
            return 'sunburst_inpaint', args
        args.update(size=spec['effective_size'], place=spec['place'])
        return 'sunburst_outpaint', args
    args.update(task=spec['operation'], seconds=spec['seconds'], aspect=spec['aspect'], resolution=spec['resolution'])
    if spec['seed'] is not None:
        args['seed'] = spec['seed']
    if spec['operation'] == 'frames':
        args['first'] = refs[0]
        if len(refs) > 1:
            args['last'] = refs[1]
    elif spec['operation'] == 'reference':
        args['refs'] = refs
    elif spec['operation'] in ('edit', 'extend'):
        args['video'] = refs[0]
    return 'omni_video', args


def background_mask(ws, row: dict, cutout: dict, cutout_path: Path, placed: list, staging: Path) -> dict:
    """Inverse local cutout alpha in the original source coordinate system."""
    source = row['prepared']['inputs'][0]
    W, H = source['width'], source['height']
    if not isinstance(placed, list) or len(placed) != 4 or any(type(value) is not int for value in placed):
        raise RuntimeError('The generation tool did not return an exact local cutout placement.')
    x, y, w, h = placed
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > W or y + h > H:
        raise RuntimeError('The generation tool returned a cutout outside its source.')
    with Image.open(cutout_path) as image:
        if image.size != (w, h):
            raise RuntimeError('The generation tool returned a cutout that does not match its placement.')
        alpha = np.array(image.convert('RGBA').getchannel('A'))
    foreground = np.zeros((H, W), np.uint8)
    foreground[y:y + h, x:x + w] = alpha
    rgba = np.full((H, W, 4), 255, np.uint8)
    rgba[..., 3] = 255 - foreground
    target = staging / 'background-mask.png'
    Image.fromarray(rgba).save(target, format='PNG')
    receipt = 'matte.background_mask'
    ref = Registry(ws.assets).register(target, slot=f"studio-mask-{row['id']}", kind='image', receipt=receipt,
        derived_from=[{'kind': 'asset', 'ref': source['ref'], 'sha256': source['sha256']},
                      {'kind': 'asset', 'ref': cutout['ref'], 'sha256': cutout['sha256']}],
        fit={'native_size': [W, H], 'op': 'inverse_alpha', 'placed_in_source': placed,
             'editable': 'background alpha > 127'})
    path = f"{row['output_folder']}/background-mask.png"
    publish(ws, path, target)
    return {'path': path, 'ref': ref, 'sha256': sha256_file(target), 'size': target.stat().st_size,
            'mime': 'image/png', 'width': W, 'height': H, 'provenance': 'segmented', 'receipt': receipt,
            'source_ref': source['ref'], 'source_sha256': source['sha256'],
            'editable_pixels': int((rgba[..., 3] > 127).sum()),
            'editable': 'background alpha > 127'}


def execute(row: dict, *, registry=None, gateway=None) -> dict:
    ws = Workspace(Path(row['request_root']))
    registry = registry or build_registry()
    local_matte = row['operation'] == 'remove_background' and row['input'].get('background_method') == 'matte'
    gateway = gateway or (Gateway(ws, actor='studio') if local_matte else
                          StudioGateway(ws, kind=row['kind'], model=row['input']['model']))
    name, args = tool_call(row)
    result = registry.call(ToolContext(ws=ws, agent='studio', role='job_lead', gateway=gateway), name, args)
    if result.is_error:
        # Provider responses can include credentials, request headers or host
        # paths. Public errors deliberately avoid copying arbitrary SDK strings.
        raise RuntimeError('The generation tool failed. Check provider access or retry with different settings.')
    if isinstance(result.data, dict) and result.data.get('parked'):
        raise RuntimeError('The policy requires approval. Resolve its request before starting a new studio job.')
    folder = directory(ws, row['output_folder'])
    staging = directory(ws, f".studio/staging/{row['id']}/results")
    asset_registry = Registry(ws.assets)
    refs = list(dict.fromkeys(ref for ref in result.artifacts if ref.startswith('slot:')))
    if not refs:
        raise RuntimeError('The generation tool returned no saved output.')
    artifacts = []
    for index, ref in enumerate(refs):
        asset = asset_registry.get(ref)
        source = ws.rel(asset['path'])
        target = staging / f'output-{index + 1}{Path(source).suffix.lower()}'
        with open_file(ws, source) as handle, target.open('xb') as out:
            shutil.copyfileobj(handle, out)
        info = media_info(target, 'video' if row['kind'] == 'video' else 'image', verify_frames=row['kind'] == 'video')
        if row['kind'] == 'video' and (info['seconds'] <= 0 or info['width'] <= 0 or info['height'] <= 0):
            raise RuntimeError('The generated video did not have valid media metadata.')
        public_path = ws.rel(folder / target.name)
        publish(ws, public_path, target)
        artifacts.append({'path': public_path, 'ref': ref,
                          'sha256': sha256_file(target), 'size': target.stat().st_size,
                          'provenance': asset['prov'], 'receipt': asset['receipt'], **info})
    mask = None
    if local_matte:
        mask = background_mask(ws, row, artifacts[0], staging / Path(artifacts[0]['path']).name,
                               (result.data or {}).get('placed_in_source'), staging)
    receipt = {'job_id': row['id'], 'created': now_iso(), 'kind': row['kind'], 'operation': row['operation'],
               'tool': name, 'requested': row['input'], 'inputs': row['prepared'],
               'artifacts': artifacts, 'background_mask': mask, 'tool_result': result.data}
    receipt_path = folder / 'receipt.json'
    publish(ws, ws.rel(receipt_path), canonical_json(receipt).encode())
    Ledger(ws.ledger / 'ledger.jsonl').append('studio.generated', 'studio',
        {'job_id': row['id'], 'tool': name, 'artifacts': artifacts, 'background_mask': mask,
         'receipt_path': ws.rel(receipt_path)})
    warnings = []
    for key in ('warnings', 'notes'):
        values = (result.data or {}).get(key, [])
        if isinstance(values, list):
            warnings.extend(value for value in values if isinstance(value, str) and value)
    if local_matte:
        warnings.append('Local alpha is an approximation. Inspect the full-size background mask before using it to inpaint.')
        if not mask['editable_pixels']:
            warnings.append('No editable background was detected. Paint a mask manually if needed.')
    elif row['operation'] == 'remove_background':
        warnings.append('This model-generated cutout may be redrawn; no source-aligned background mask is inferred from it.')
    return {'artifacts': artifacts, 'receipt_path': ws.rel(receipt_path),
            'model': 'local-matte' if local_matte else row['input']['model'], 'background_mask': mask,
            'warnings': list(dict.fromkeys(warnings)),
            'tool_result': result.data}


def watch_parent(descriptor: int) -> None:
    """An inherited pipe closes on a hard host exit, ending the owned process group."""
    def wait():
        try:
            while os.read(descriptor, 1):
                pass
        finally:
            os.close(descriptor)
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGTERM)
    threading.Thread(target=wait, name='studio-parent-watch', daemon=True).start()


def main() -> int:
    if len(sys.argv) > 3 and sys.argv[2] == '--watch-fd':
        watch_parent(int(sys.argv[3]))
    path = Path(sys.argv[1])
    try:
        row = read_json(path)
        result = execute(row)
        payload = {'ok': True, 'result': result}
    except Exception as exc:
        # Only errors authored by this module are public. No arbitrary SDK text.
        safe = str(exc) if type(exc) is RuntimeError and str(exc).startswith(('The generation tool', 'The policy requires', 'The generated video')) else 'Generation failed. Check provider access and try a new job.'
        payload = {'ok': False, 'error': safe}
    write_json(path.with_suffix('.result'), payload)
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
