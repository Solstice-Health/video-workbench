"""Studio controls describe only the adapters already supported by the toolkit."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vwb.config import provider_status, slot
from vwb.gateway.core import Gateway
from vwb.gen.images import valid_size

IMAGE_OPS = ['create', 'edit', 'remove_background', 'inpaint', 'outpaint']
VIDEO_OPS = ['text', 'frames', 'reference', 'edit', 'extend']


def model_configs(kind: str) -> list[dict]:
    primary = slot('img.sunburst' if kind == 'image' else 'vid.omni')
    provider = 'openai' if kind == 'image' else 'google'
    configs = [primary] + [dict(primary, **row) for row in primary.get('fallbacks', [])]
    return [dict(row, fallbacks=[]) for row in configs if row.get('provider') == provider]


def capabilities() -> dict:
    available = {row['id']: row['configured'] for row in provider_status()}
    def models(kind):
        return [{'id': cfg['model'], 'label': cfg['model'], 'default': i == 0}
                for i, cfg in enumerate(model_configs(kind))]
    return {
        'image': {'available': available.get('openai', False), 'provider': 'openai', 'models': models('image'),
                  'operations': IMAGE_OPS, 'quality': ['low', 'medium', 'high', 'xhigh', 'max'],
                  'default_quality': 'high', 'default_size': [1024, 1024],
                  'background_methods': [
                      {'id': 'matte', 'label': 'Preserve source (local)', 'available': True},
                      {'id': 'sunburst', 'label': 'Regenerate cutout (Sunburst)', 'available': available.get('openai', False)}],
                  'default_background_method': 'matte',
                  'size_constraints': {'multiple': 16, 'max_edge': 3840, 'min_pixels': 655360,
                                       'max_pixels': 8294400, 'max_aspect': 3, 'normalizes': True},
                  'mask': {'format': 'png', 'editable': 'alpha > 127', 'must_match_source': True},
                  'notes': ['Edit uses reference-guided generation; inpaint uses a pixel mask.',
                            'Local background removal preserves source colors and produces an approximate reusable mask.',
                            'Sunburst background removal redraws the named subject; it does not produce a source-aligned mask.',
                            'Inpaint retains source dimensions and every pixel outside the painted mask.',
                            'Inpaint keeps the original alpha channel; use outpaint to extend into empty canvas.',
                            'Outpaint preserves the placed source except for a 32-pixel blended edge.']},
        'video': {'available': available.get('google', False), 'provider': 'google', 'models': models('video'),
                  'operations': VIDEO_OPS, 'seconds': {'min': 3, 'max': 10, 'default': 6},
                  'resolutions': ['720p', '1080p'], 'aspects': ['9:16', '16:9'],
                  'default_resolution': '1080p', 'default_aspect': '9:16',
                  'notes': ['Only models configured for the Omni Interactions adapter are offered.',
                            'Stopping a local job cannot guarantee cancellation or refund of a provider request.']},
        'uploads': {'image_max_bytes': 40 * 1024**2, 'video_max_bytes': 250 * 1024**2,
                    'image_formats': ['png', 'jpeg', 'webp'], 'mask_format': 'png'},
    }


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    upload_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    path: str | None = Field(default=None, max_length=4096)
    ref: str | None = Field(default=None, pattern=r'^slot:[A-Za-z0-9][A-Za-z0-9_.-]*@[1-9][0-9]*$')

    @model_validator(mode='after')
    def one(self):
        if sum(v is not None for v in (self.upload_id, self.path, self.ref)) != 1:
            raise ValueError('Choose one upload, workspace path or pinned asset reference per input.')
        return self


class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    kind: Literal['image', 'video']
    operation: str
    prompt: str = Field(default='', max_length=20000)
    model: str | None = Field(default=None, max_length=180)
    quality: Literal['low', 'medium', 'high', 'xhigh', 'max'] = 'high'
    size: list[int] = Field(default_factory=lambda: [1024, 1024], min_length=2, max_length=2)
    transparent: bool = False
    allow_text: bool = True
    inputs: list[Input] = Field(default_factory=list, max_length=16)
    mask: Input | None = None
    place: list[int] | None = Field(default=None, min_length=4, max_length=4)
    box: list[int] | None = Field(default=None, min_length=4, max_length=4)
    subject: str = Field(default='', max_length=2000)
    background_method: Literal['matte', 'sunburst'] = 'matte'
    seconds: int = Field(default=6, ge=3, le=10)
    resolution: Literal['720p', '1080p'] = '1080p'
    aspect: Literal['9:16', '16:9'] = '9:16'
    seed: int | None = Field(default=None, ge=0, le=2147483647)

    @model_validator(mode='after')
    def controls(self):
        ops = IMAGE_OPS if self.kind == 'image' else VIDEO_OPS
        if self.operation not in ops:
            raise ValueError('This operation is not supported by the selected studio.')
        local_matte = self.kind == 'image' and self.operation == 'remove_background' and self.background_method == 'matte'
        if local_matte:
            self.model = None
        else:
            configs = model_configs(self.kind)
            if not configs:
                raise ValueError('No models are configured for this studio adapter.')
            self.model = self.model or configs[0]['model']
            if self.model not in [c['model'] for c in configs]:
                raise ValueError('Select a model offered by the configured studio adapter.')
        if any(not 16 <= value <= 3840 for value in self.size):
            raise ValueError('Image dimensions must be between 16 and 3840 pixels.')
        if max(self.size) / min(self.size) > 3:
            raise ValueError('Image aspect ratio must be between 1:3 and 3:1.')
        count = len(self.inputs)
        if self.kind == 'image':
            if self.operation != 'create' and not count:
                raise ValueError('This operation needs an input image.')
            if self.operation in ('inpaint', 'outpaint', 'remove_background') and count != 1:
                raise ValueError('This operation needs exactly one input image.')
            if self.operation == 'inpaint' and self.mask is None:
                raise ValueError('Paint a mask first. Transparent pixels are retained.')
            if self.operation == 'remove_background' and self.background_method == 'sunburst' and not self.subject.strip():
                raise ValueError('Describe the subject to keep.')
        else:
            valid = (count == 0 if self.operation == 'text' else 1 <= count <= 2 if self.operation == 'frames'
                     else 1 <= count <= 3 if self.operation == 'reference' else count == 1)
            if not valid:
                raise ValueError('The selected video operation has the wrong number of inputs.')
        if self.operation != 'remove_background' and not self.prompt.strip():
            raise ValueError('Describe the result you want.')
        if self.mask is not None and not (self.kind == 'image' and self.operation == 'inpaint'):
            raise ValueError('Masks are supported only by image inpaint.')
        if self.place is not None and not (self.kind == 'image' and self.operation == 'outpaint'):
            raise ValueError('Placement is supported only by image outpaint.')
        if self.box is not None and not (self.kind == 'image' and self.operation == 'remove_background'):
            raise ValueError('A subject region is supported only by background removal.')
        return self

    def public(self) -> dict:
        data = self.model_dump(mode='json')
        data['effective_size'] = list(valid_size(*self.size))
        return data


class StudioGateway(Gateway):
    def __init__(self, ws, *, kind: str, model: str):
        super().__init__(ws, actor='studio')
        self.studio_slot = 'img.sunburst' if kind == 'image' else 'vid.omni'
        self.selected = next((c for c in model_configs(kind) if c['model'] == model), None)
        if self.selected is None:
            raise ValueError('The selected studio model is no longer configured.')

    def resolve_slot(self, slot_name: str) -> dict:
        # Explicit selection stays on that model, including retries. It must not
        # silently substitute a different model chosen by a background fallback.
        return dict(self.selected) if slot_name == self.studio_slot else super().resolve_slot(slot_name)
