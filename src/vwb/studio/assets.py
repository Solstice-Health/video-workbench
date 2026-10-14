"""Validated input snapshots and public studio artifacts; no arbitrary host reads."""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from vwb.studio.models import Input
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import read_json, write_json
from vwb.web.workspace_files import open_file
from vwb.workspace.registry import Registry
from vwb.workspace.request import now_iso

IMAGE_MAX = 40 * 1024**2
VIDEO_MAX = 250 * 1024**2
VIDEO_SUFFIXES = {'.mp4', '.mov', '.m4v', '.webm', '.mkv'}


def directory(ws, relative: str) -> Path:
    descriptor = _directory_fd(ws, relative)
    os.close(descriptor)
    return ws.root / relative


def _directory_fd(ws, relative: str) -> int:
    """Create/open each directory relative to a pinned no-follow descriptor."""
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(part in ('', '.', '..') for part in parts):
        raise ValueError('Invalid studio directory.')
    descriptor = os.open(ws.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise ValueError('Studio directories must be regular folders, without symbolic links.') from None


def publish(ws, relative: str, source: Path | bytes) -> None:
    """Publish immutable bytes without reopening agent-controlled parent paths."""
    path = Path(relative)
    parent = _directory_fd(ws, path.parent.as_posix())
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, 'wb') as out:
            if isinstance(source, bytes):
                out.write(source)
            else:
                with source.open('rb') as handle:
                    shutil.copyfileobj(handle, out, 1024**2)
    finally:
        os.close(parent)


def media_info(path: Path, kind: str, *, verify_frames: bool = False) -> dict:
    if kind != 'video':
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.load()
                if image.width * image.height > 16_777_216 or max(image.size) > 8192:
                    raise ValueError('Input images must be at most 16 megapixels and 8192 pixels per edge.')
                return {'width': image.width, 'height': image.height, 'mime': 'image/png'}
    proc = subprocess.run(['ffprobe', '-v', 'error', *(['-count_frames'] if verify_frames else []),
                           '-show_streams', '-show_format', '-of', 'json', str(path)],
                          capture_output=True, timeout=25, check=True)
    data = json.loads(proc.stdout)
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not video:
        raise ValueError('The input has no video stream.')
    duration = float(video.get('duration') or data.get('format', {}).get('duration') or 0)
    width, height = int(video['width']), int(video['height'])
    if not (0 < duration <= 600 and 0 < width <= 8192 and 0 < height <= 8192):
        raise ValueError('Input videos must be at most ten minutes with valid dimensions.')
    frames = int(video.get('nb_read_frames') or video.get('nb_frames') or 0)
    if verify_frames and frames <= 0:
        raise ValueError('The generated video has no decodable frames.')
    return {'width': width, 'height': height, 'seconds': duration,
            'frames': frames, 'mime': mimetypes.guess_type(path.name)[0] or 'video/mp4'}


def normalize_image(source: Path, target: Path, *, mask: bool = False) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(source) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP') or (mask and image.format != 'PNG'):
                raise ValueError('Use PNG, JPEG or WebP images; masks must be PNG.')
            if image.width * image.height > 16_777_216 or max(image.size) > 8192:
                raise ValueError('Input images must be at most 16 megapixels and 8192 pixels per edge.')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Choose a still image, not an animation.')
            if mask and 'A' not in image.getbands():
                raise ValueError('Masks need an alpha channel: opaque selected pixels, transparent retained pixels.')
            result = ImageOps.exif_transpose(image).convert('RGBA')
            if mask and not result.getchannel('A').point(lambda v: 255 if v > 127 else 0).getbbox():
                raise ValueError('The mask is empty. Paint an area first.')
            result.save(target, format='PNG')
            return {'width': result.width, 'height': result.height, 'mime': 'image/png'}


def upload(ws, stream, length: int, *, name: str, kind: str) -> dict:
    if kind not in ('image', 'mask', 'video'):
        raise ValueError('Choose image, mask or video for this upload.')
    maximum = VIDEO_MAX if kind == 'video' else IMAGE_MAX
    if not 0 < length <= maximum:
        raise ValueError(f'This upload must be between 1 and {maximum} bytes.')
    suffix = Path(name).suffix.lower()
    if kind == 'video' and suffix not in VIDEO_SUFFIXES:
        raise ValueError('Use MP4, MOV, M4V, WebM or MKV video.')
    uid = uuid.uuid4().hex
    folder = directory(ws, f'.studio/staging/{uid}')
    raw = folder / 'upload.bin'
    target = folder / ('input' + (suffix if kind == 'video' else '.png'))
    try:
        remaining = length
        with raw.open('xb') as handle:
            while remaining:
                data = stream.read(min(1024**2, remaining))
                if not data:
                    raise ValueError('The upload was interrupted. Upload it again.')
                handle.write(data)
                remaining -= len(data)
        if kind == 'video':
            os.replace(raw, target)
            metadata = media_info(target, kind)
        else:
            metadata = normalize_image(raw, target, mask=kind == 'mask')
        public_path = f'work/studio/inputs/{uid}/{target.name}'
        publish(ws, public_path, target)
        result = {'upload_id': uid, 'path': public_path, 'kind': kind,
                  'name': Path(name.replace('\\', '/')).name[:240], 'size': target.stat().st_size,
                  'sha256': sha256_file(target), 'created': now_iso(), **metadata}
        private = directory(ws, '.studio/uploads')
        write_json(private / f'{uid}.json', result)
        return result
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning, subprocess.SubprocessError, json.JSONDecodeError):
        target.unlink(missing_ok=True)
        raise ValueError('The uploaded media could not be decoded safely.') from None
    finally:
        raw.unlink(missing_ok=True)


def snapshot_input(ws, source: Input, folder: Path, index: str, *, kind: str) -> dict:
    receipt = 'studio.reference'
    original_ref = None
    expected_hash = None
    if source.upload_id:
        metadata = read_json(directory(ws, '.studio/uploads') / f'{source.upload_id}.json')
        if not metadata or metadata['kind'] != kind:
            raise ValueError('Choose an uploaded input of the correct media type.')
        path = metadata['path']
        receipt = 'human.upload'
        expected_hash = metadata['sha256']
    elif source.ref:
        row = Registry(ws.assets).get(source.ref)
        if (row['kind'] == 'video') != (kind == 'video'):
            raise ValueError('The asset has the wrong media type.')
        path = Path(row['path']).relative_to(ws.root).as_posix()
        receipt, original_ref = row['receipt'], source.ref
        expected_hash = row['sha256']
    else:
        path = source.path
    suffix = Path(path).suffix.lower() if kind == 'video' else '.png'
    if kind == 'video' and suffix not in VIDEO_SUFFIXES:
        raise ValueError('Choose a supported video input.')
    private = directory(ws, f'.studio/staging/{folder.parent.name}')
    raw = private / f'{index}.original'
    target = private / f'{index}{suffix}'
    with open_file(ws, path) as stream:
        if not 0 < os.fstat(stream.fileno()).st_size <= (VIDEO_MAX if kind == 'video' else IMAGE_MAX):
            raise ValueError('This input exceeds the studio file-size limit.')
        with raw.open('xb') as handle:
            shutil.copyfileobj(stream, handle, 1024**2)
    try:
        source_digest = sha256_file(raw)
        if expected_hash and source_digest != expected_hash:
            raise ValueError('The input changed after its receipt was saved. Upload it again or choose a new reference.')
        if kind == 'video':
            os.replace(raw, target)
            info = media_info(target, kind)
            if info['seconds'] > 10.1:
                raise ValueError('Omni edit and extend inputs must be at most 10 seconds.')
        else:
            info = normalize_image(raw, target, mask=kind == 'mask')
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError('The selected workspace media could not be decoded safely.') from None
    finally:
        raw.unlink(missing_ok=True)
    digest = sha256_file(target)
    public_path = (folder / target.name).relative_to(ws.root).as_posix()
    publish(ws, public_path, target)
    lineage = {'path': path, 'sha256': source_digest, 'kind': 'studio_input'}
    if original_ref:
        lineage['ref'] = original_ref
    ref = Registry(ws.assets).register(target, slot=f'studio-input-{folder.parent.name}-{index}',
                                      kind='video' if kind == 'video' else 'image', receipt=receipt,
                                      derived_from=[lineage], fit=info)
    return {'ref': ref, 'path': public_path, 'source': path,
            'sha256': digest, 'source_sha256': source_digest, 'kind': kind, **info}
