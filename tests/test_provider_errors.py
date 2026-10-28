"""Token-count diagnostics never copy untrusted provider prose or payloads."""

import httpx
import pytest

from vwb.agents.provider_errors import token_count_failure

_SAVED = "The next generation was not sent; completed work is saved."
_ECHO = (
    "sk-ant-secret-DO-NOT-PRINT private prompt: launch the confidential project "
    "/Users/private/credentials.env C:\\private\\secrets.txt "
    "c2lnbmVkLXRoaW5raW5nLXNlY3JldA== <script>alert('private')</script> "
    "Authorization: Bearer private-token"
)


def response(message, *, error_type="invalid_request_error", status=400, headers=None, **body):
    return httpx.Response(status, json={"error": {"type": error_type, "message": message}, **body},
                          headers=headers)


@pytest.mark.parametrize("message,category,location", [
    ("messages.3.content.0: Invalid `signature` in `thinking` block", "thinking signature mismatch",
     "messages.3.content.0"),
    ("messages.2.content.1.tool_use_id: unexpected tool_result without corresponding tool_use",
     "malformed tool exchange", "messages.2.content.1.tool_use_id"),
    ("messages.0.content.2.source.data: image exceeds the size limit", "image size limit exceeded",
     "messages.0.content.2.source.data"),
    ("messages.0: A maximum of 100 images is allowed", "image count limit exceeded", "messages.0"),
    ("tools.1.input_schema: invalid JSON schema", "invalid schema or configuration", "tools.1.input_schema"),
    ("thinking.budget_tokens: Input should be greater than 1024", "invalid schema or configuration",
     "thinking.budget_tokens"),
    ("temperature: Input should be less than or equal to 1", "invalid schema or configuration", "temperature"),
    ("messages.7: prompt is too long: 999999 tokens", "context too long", "messages.7"),
])
def test_validation_categories_keep_only_safe_location_and_request_id(message, category, location):
    result = token_count_failure(response(message + " " + _ECHO, headers={"request-id": "req_ABC-123_xyz"}))
    assert result == (
        "Anthropic rejected the token-count request (HTTP 400; invalid_request_error): "
        f"{category} at {location}. {_SAVED} Request ID: req_ABC-123_xyz."
    )


@pytest.mark.parametrize("error_type,status,prefix,category", [
    ("authentication_error", 401, "Anthropic could not authenticate the token-count request", "authentication failed"),
    ("permission_error", 403, "Anthropic denied permission for the token-count request", "permission denied"),
    ("not_found_error", 404, "Anthropic could not find the requested token-count resource", "requested resource not found"),
    ("request_too_large", 413, "Anthropic rejected the token-count request as too large", "request size limit exceeded"),
    ("rate_limit_error", 429, "Anthropic rate-limited the token-count request", "rate limit reached"),
    ("api_error", 500, "Anthropic token counting is temporarily unavailable", "provider service error"),
    ("overloaded_error", 529, "Anthropic token counting is temporarily unavailable", "provider temporarily unavailable"),
])
def test_known_error_types_never_echo_message(error_type, status, prefix, category):
    result = token_count_failure(response("messages.3.content.0: " + _ECHO,
                                         error_type=error_type, status=status))
    assert result == f"{prefix} (HTTP {status}; {error_type}): {category}. {_SAVED}"


def test_unknown_validation_message_has_stable_fallback_without_location():
    result = token_count_failure(response("messages.3.content.0: " + _ECHO))
    assert result == f"Anthropic rejected the token-count request (HTTP 400; invalid_request_error). {_SAVED}"


@pytest.mark.parametrize("error_type", [None, False, 5, [], {}, _ECHO, "invalid_request_error\n" + _ECHO])
def test_unknown_or_malformed_error_type_does_not_classify_or_leak(error_type):
    result = token_count_failure(response("messages.3.content.0: Invalid thinking signature " + _ECHO,
                                         error_type=error_type))
    assert result == f"Anthropic rejected the token-count request (HTTP 400). {_SAVED}"


@pytest.mark.parametrize("message", [None, False, 5, [], {}, ["invalid thinking signature", _ECHO]])
def test_non_string_message_is_safe(message):
    result = token_count_failure(response(message))
    assert result == f"Anthropic rejected the token-count request (HTTP 400; invalid_request_error). {_SAVED}"


@pytest.mark.parametrize("location", [
    "messages.1234567.content.0", "messages.1.content.0.private_secret", "messages.1.content.0/secret",
    "/Users/private/messages.1.content.0", "C:\\private\\messages.1.content.0",
    "messages.1.content.0_credential", "messages.1.content.0-secret", "messages.1.content.0[secret]",
    "messages.1.content.0.\u00e9secret", "\u00e9messages.1.content.0", "messages.-1.content.0",
])
def test_locations_cannot_be_partial_paths_or_unbounded_indices(location):
    result = token_count_failure(response(f"{location}: Invalid thinking signature {_ECHO}"))
    assert result == (
        "Anthropic rejected the token-count request (HTTP 400; invalid_request_error): "
        f"thinking signature mismatch. {_SAVED}"
    )


@pytest.mark.parametrize("request_id", [
    "req_A", "req_01234abc-XYZ_under", "req_" + "a" * 124,
])
def test_safe_request_id_from_header_or_body(request_id):
    for provider_response in (response("unknown", headers={"Request-ID": request_id}),
                              response("unknown", request_id=request_id)):
        assert token_count_failure(provider_response).endswith(f"Request ID: {request_id}.")


@pytest.mark.parametrize("request_id", [
    "req_", "req_" + "a" * 125, " req_safe", "req_safe ", "req_safe\n", "req_safe/path",
    "req_safe.private", "req_safe<script>", "req_safe\r\nAuthorization: secret", "req_\u00e9secret", _ECHO,
    None, False, 42, [], {},
])
def test_unsafe_body_request_id_is_omitted(request_id):
    result = token_count_failure(response("unknown", request_id=request_id))
    assert result == f"Anthropic rejected the token-count request (HTTP 400; invalid_request_error). {_SAVED}"


@pytest.mark.parametrize("request_id", ["req_safe/path", "req_safe\n", "req_" + "a" * 125, _ECHO])
def test_invalid_header_uses_only_validated_body_request_id(request_id):
    result = token_count_failure(response("unknown", headers={"request-id": request_id}, request_id="req_body"))
    assert result.endswith("Request ID: req_body.")
    assert request_id not in result


def test_header_request_id_takes_precedence_and_other_headers_are_ignored():
    result = token_count_failure(response("unknown", request_id="req_body", headers={
        "request-id": "req_header", "authorization": _ECHO, "x-api-key": _ECHO, "x-request-id": _ECHO,
    }))
    assert result.endswith("Request ID: req_header.")
    assert "req_body" not in result
    assert _ECHO not in result


@pytest.mark.parametrize("body", [None, [], False, 5, _ECHO, {"error": []}, {"error": _ECHO},
                                      {"error": None}, {"error": {"message": _ECHO}}])
def test_unexpected_json_body_shapes_are_safe(body):
    result = token_count_failure(httpx.Response(400, json=body))
    assert result == f"Anthropic rejected the token-count request (HTTP 400). {_SAVED}"


@pytest.mark.parametrize("body", [b"", b"not json", b"\xff", b'{"error":', b"[" * 2000 + b"]" * 2000,
                                      _ECHO.encode(), b"x" * 65537])
def test_malformed_or_oversized_body_keeps_header_id_without_echo(body):
    result = token_count_failure(httpx.Response(400, content=body, headers={"request-id": "req_header"}))
    assert result == f"Anthropic rejected the token-count request (HTTP 400). {_SAVED} Request ID: req_header."


def test_oversized_message_is_not_classified_or_copied():
    result = token_count_failure(response("messages.0: invalid thinking signature " + "x" * 4096 + _ECHO))
    assert result == f"Anthropic rejected the token-count request (HTTP 400; invalid_request_error). {_SAVED}"


@pytest.mark.parametrize("status,description", [
    (408, "Anthropic token counting timed out"),
    (429, "Anthropic rate-limited the token-count request"),
    (502, "Anthropic token counting is temporarily unavailable"),
    (503, "Anthropic token counting is temporarily unavailable"),
    (418, "Anthropic token counting failed"),
])
def test_status_remains_useful_without_json(status, description):
    result = token_count_failure(httpx.Response(status, text=_ECHO))
    assert result == f"{description} (HTTP {status}). {_SAVED}"


def test_unread_stream_is_never_read():
    class UnreadStream(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("The diagnostic helper must not read a stream")

    provider_response = httpx.Response(503, stream=UnreadStream(), headers={"request-id": "req_offline"})
    assert token_count_failure(provider_response) == (
        f"Anthropic token counting is temporarily unavailable (HTTP 503). {_SAVED} Request ID: req_offline."
    )
    assert not provider_response.is_stream_consumed
