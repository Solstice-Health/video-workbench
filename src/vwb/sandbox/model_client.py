"""Small stdlib-only SDK mounted in opted-in sandboxes as ``vwb_models``.

The endpoint belongs to the trusted relay inside this container. Provider keys,
host paths and vendor endpoints are deliberately absent from this client.
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024


class ModelError(RuntimeError):
    """A model tool rejected or could not complete this request."""


def _call(tool, arguments):
    endpoint = os.environ.get('VWB_MODEL_ENDPOINT', '')
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.path != '/request' or parsed.query or parsed.fragment or parsed.username):
        raise ModelError('Model access is unavailable. Run sandbox_run with models=true.')
    data = json.dumps({'tool': tool, 'arguments': arguments}, allow_nan=False).encode('utf-8')
    if len(data) > MAX_REQUEST_BYTES:
        raise ModelError('Model request exceeds 32 MiB. Use workspace file references instead.')
    request = Request(endpoint, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        # Public-network mode sets HTTP_PROXY. This endpoint must always remain
        # on the container's loopback, independently of those proxy settings.
        with build_opener(ProxyHandler({})).open(request, timeout=1800) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise ModelError(f'Model bridge refused the request (HTTP {exc.code}).') from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ModelError(f'Model bridge unavailable: {type(exc).__name__}.') from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelError('Model bridge response exceeds its transport limit.')
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ModelError('Invalid model bridge response.') from None
    if not isinstance(result, dict) or result.get('is_error'):
        raise ModelError(result.get('text', 'Model request failed.') if isinstance(result, dict)
                         else 'Invalid model bridge response.')
    return result.get('data') if result.get('data') is not None else {'text': result.get('text', '')}


def providers():
    """List configured providers and supported operations without revealing keys."""
    return _call('model_providers', {})


def catalog(provider, query='', modality='', limit=30, offset=0):
    """Discover current model IDs; page with limit/offset and filter capabilities."""
    return _call('model_catalog', {'provider': provider, 'query': query, 'modality': modality,
                                   'limit': limit, 'offset': offset})


def request(provider, operation, payload, purpose=''):
    """Call a supported native provider operation through the host gateway.

    Use relative workspace file references in payloads, e.g.
    ``{'$file': 'input.png', 'encoding': 'data_url'}``. The returned receipt holds
    generated workspace paths and a saved response path for larger outputs.
    """
    return _call('model_request', {'provider': provider, 'operation': operation,
                                  'payload': payload, 'purpose': purpose})
