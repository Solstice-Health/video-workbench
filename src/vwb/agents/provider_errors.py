"""Public diagnostics from untrusted provider errors; never echo provider prose."""

import re

import httpx

_MAX_BODY_BYTES = 65536
_MAX_MESSAGE_CHARS = 4096
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9_-]{1,124}\Z")
_ERROR_TYPES = {
    "invalid_request_error": "",
    "authentication_error": "authentication failed",
    "permission_error": "permission denied",
    "not_found_error": "requested resource not found",
    "request_too_large": "request size limit exceeded",
    "rate_limit_error": "rate limit reached",
    "api_error": "provider service error",
    "overloaded_error": "provider temporarily unavailable",
}
_STATUS_MESSAGES = {
    400: "Anthropic rejected the token-count request",
    401: "Anthropic could not authenticate the token-count request",
    403: "Anthropic denied permission for the token-count request",
    404: "Anthropic could not find the requested token-count resource",
    408: "Anthropic token counting timed out",
    413: "Anthropic rejected the token-count request as too large",
    422: "Anthropic rejected the token-count request",
    429: "Anthropic rate-limited the token-count request",
}

# Only canonical field names and small numeric indices may leave the helper.
# Boundaries prevent extracting a safe-looking prefix from an arbitrary path.
_LOCATION = re.compile(
    r"(?<![\w./\\\[\]-])(?:"
    r"messages\.[0-9]{1,6}(?:\.content\.[0-9]{1,6}){0,2}"
    r"(?:\.(?:role|type|text|thinking|signature|tool_use_id|name|input|"
    r"source(?:\.(?:type|media_type|data))?))?"
    r"|tools\.[0-9]{1,6}(?:\.(?:name|description|input_schema))?"
    r"|system\.[0-9]{1,6}(?:\.(?:type|text))?"
    r"|thinking\.(?:type|budget_tokens)"
    r"|(?:model|max_tokens|temperature|top_p|top_k|tool_choice)(?=\s*:)"
    r")(?![\w./\\\[\]-])"
)


def _validation_category(message: str) -> str:
    """Recognize a category, returning only a fixed explanation."""
    text = message.lower().replace("`", "")
    if "thinking" in text and "signature" in text and any(
        marker in text for marker in ("invalid", "mismatch", "does not match", "missing", "required",
                                      "could not verify", "verification failed", "must match")
    ):
        return "thinking signature mismatch"
    if any(marker in text for marker in ("tool_use", "tool_result")) and any(
        marker in text for marker in ("missing", "unexpected", "without", "corresponding", "must", "required")
    ):
        return "malformed tool exchange"
    if "image" in text:
        if any(marker in text for marker in ("too many images", "image count", "number of images")) or re.search(
            r"\bmax(?:imum)?(?: of)? [0-9]{1,6} images\b", text
        ):
            return "image count limit exceeded"
        if any(marker in text for marker in ("too large", "exceed", "dimension", "megapixel", "size limit")):
            return "image size limit exceeded"
    if any(marker in text for marker in (
        "context too long", "context is too long", "prompt is too long", "input is too long",
        "exceeds the context", "exceeds context", "maximum context length", "context window exceeded",
    )):
        return "context too long"
    invalid = any(marker in text for marker in (
        "invalid", "not valid", "unsupported", "not supported", "must", "required", "should be",
        "extra inputs are not permitted",
    ))
    if invalid and ("schema" in text or "configuration" in text or "config" in text or
                    "extra inputs are not permitted" in text or _LOCATION.search(message)):
        return "invalid schema or configuration"
    return ""


def _error_body(response: httpx.Response) -> dict:
    # Do not read an unread stream: this helper must never perform network I/O.
    try:
        if len(response.content) > _MAX_BODY_BYTES:
            return {}
        body = response.json()
    except (ValueError, RecursionError, httpx.ResponseNotRead):
        return {}
    return body if isinstance(body, dict) else {}


def token_count_failure(response: httpx.Response) -> str:
    """Format an Anthropic count_tokens failure for public UI and logs.

    Provider messages are used only to select fixed categories. Unknown types,
    arbitrary fields, response headers and raw body text are never interpolated.
    Only a validated request ID and an allowlisted field location are retained.
    """
    body = _error_body(response)
    error = body.get("error")
    error = error if isinstance(error, dict) else {}
    error_type = error.get("type")
    error_type = error_type if isinstance(error_type, str) and error_type in _ERROR_TYPES else ""
    category = _ERROR_TYPES.get(error_type, "")
    location = ""
    message = error.get("message")
    if (error_type == "invalid_request_error" and isinstance(message, str)
            and len(message) <= _MAX_MESSAGE_CHARS):
        category = _validation_category(message)
        if category and (match := _LOCATION.search(message)):
            location = f" at {match.group()}"

    status = response.status_code
    prefix = _STATUS_MESSAGES.get(status, "Anthropic token counting is temporarily unavailable"
                                  if 500 <= status <= 599 else "Anthropic token counting failed")
    result = f"{prefix} (HTTP {status}" + (f"; {error_type}" if error_type else "") + ")"
    if category:
        result += f": {category}{location}"
    result += ". The next generation was not sent; completed work is saved."
    for request_id in (response.headers.get("request-id"), body.get("request_id")):
        if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id):
            result += f" Request ID: {request_id}."
            break
    return result
