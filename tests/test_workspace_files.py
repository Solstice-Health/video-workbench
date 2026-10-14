"""Public file exploration never grants access to private request/runtime state."""
from __future__ import annotations

import base64
import json
import os
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from vwb.web import workspace_files as files
from vwb.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-files")
    workspace.root.mkdir()
    for name in (*files.PUBLIC_ROOTS, "memory", "ledger", "swarm", "agent", ".browser"):
        (workspace.root / name).mkdir()
    (workspace.root / "request.json").write_text('{"chat": "private chat"}')
    return workspace


def put(ws, path, content=b"example"):
    target = ws.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode())
    return target


def test_root_contains_only_existing_public_directories(ws):
    put(ws, "request.json", "secret")
    put(ws, "stray.txt", "not part of the public artifact tree")
    result = files.listing(ws)
    assert {entry["name"] for entry in result["entries"]} == files.PUBLIC_ROOTS
    assert all(entry["kind"] == "directory" and not entry["is_file"] for entry in result["entries"])
    assert result["next_cursor"] is None and not result["truncated"]
    assert str(ws.root) not in json.dumps(result)


def test_allowed_roots_must_be_directories_not_root_files(ws):
    (ws.root / "sources").rmdir()
    put(ws, "sources", "root-level file")
    assert "sources" not in {entry["name"] for entry in files.listing(ws)["entries"]}
    with pytest.raises(PermissionError):
        files.metadata(ws, "sources")


@pytest.mark.parametrize("path", [
    "request.json", "memory/lead_session.json", "ledger/tools.jsonl", ".browser/profile/Default/Cookies",
    "agent/codex/auth.json", "work/.env", "work/.env.local", "work/.browser/state.json",
    "work/credentials.json", "work/credentials-export.json", "work/api_key.txt", "work/access_token.json",
    "work/id_rsa", "work/secret.pem", "work/session.json.bak", "work/cookies.sqlite", "work/oauth.json",
    "work/integrations/.env", "work/integrations/credentials/access.json", "work/memory/notes.md",
])
def test_private_paths_are_rejected_even_when_the_file_exists(ws, path):
    put(ws, path, "must not be public")
    with pytest.raises(PermissionError):
        files.metadata(ws, path)
    with pytest.raises(PermissionError):
        with files.open_file(ws, path):
            pytest.fail("Private file was opened")


def test_listing_hides_private_names_but_preserves_design_tokens(ws):
    for name in (".env", "credentials.json", "id_ed25519", "session.json", "private.key", "tokens.json", "authored.md"):
        put(ws, "work/integrations/" + name, "{}")
    result = files.listing(ws, "work/integrations")
    assert {entry["name"] for entry in result["entries"]} == {"tokens.json", "authored.md"}
    assert files.metadata(ws, "work/integrations/tokens.json")["text"] == "{}"


@pytest.mark.parametrize("path", ["../outside", "/work/a", "work/../memory/x", "work//a", "work/./a", "work/a\\b", "work/a\x00b", "work/a\nb", "work/", "work/" + "nested/" * 33 + "a"])
def test_traversal_controls_and_noncanonical_paths_are_rejected(ws, path):
    with pytest.raises((ValueError, PermissionError)):
        files.metadata(ws, path)


def test_path_length_and_page_limits_are_bounded(ws):
    with pytest.raises(ValueError):
        files.listing(ws, "work/" + "x" * files.MAX_PATH_BYTES)
    for limit in (0, files.MAX_PAGE_ENTRIES + 1, True, "10"):
        with pytest.raises(ValueError):
            files.listing(ws, "work", limit=limit)


@pytest.mark.parametrize("target_inside", [False, True])
def test_every_symlink_component_is_denied_even_for_in_root_targets(ws, tmp_path, target_inside):
    target = ws.work / "real" if target_inside else tmp_path / "outside"
    target.mkdir()
    (target / "secret.txt").write_text("not through symlinks")
    (ws.work / "linked").symlink_to(target, target_is_directory=True)
    (ws.work / "linked.txt").symlink_to(target / "secret.txt")
    assert {entry["name"] for entry in files.listing(ws, "work")["entries"]}.isdisjoint({"linked", "linked.txt"})
    for path in ("work/linked/secret.txt", "work/linked.txt"):
        with pytest.raises(PermissionError):
            files.metadata(ws, path)
    with pytest.raises(PermissionError):
        files.listing(ws, "work/linked")


def test_root_symlink_and_special_files_are_not_opened(ws, tmp_path):
    link = tmp_path / "workspace-link"
    link.symlink_to(ws.root, target_is_directory=True)
    with pytest.raises(PermissionError):
        files.listing(Workspace(link))
    os.mkfifo(ws.work / "pipe.txt")
    assert "pipe.txt" not in {entry["name"] for entry in files.listing(ws, "work")["entries"]}
    with pytest.raises(PermissionError):
        files.metadata(ws, "work/pipe.txt")


def test_open_descriptor_does_not_follow_replacement_after_validation(ws, tmp_path):
    original = put(ws, "work/read.txt", "allowed original")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside content")
    with files.open_file(ws, "work/read.txt") as handle:
        original.unlink()
        original.symlink_to(secret)
        assert handle.read() == b"allowed original"
    assert handle.closed
    with pytest.raises(PermissionError):
        files.metadata(ws, "work/read.txt")


def test_pinned_directory_cannot_be_redirected_during_walk(ws, tmp_path, monkeypatch):
    original = put(ws, "work/nested/read.txt", "allowed original")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "read.txt").write_text("outside content")
    native_open = os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        result = native_open(path, flags, *args, **kwargs)
        if path == "nested" and not replaced:
            replaced = True
            original.parent.rename(ws.work / "pinned")
            (ws.work / "nested").symlink_to(external, target_is_directory=True)
        return result

    monkeypatch.setattr(files.os, "open", racing_open)
    with files.open_file(ws, "work/nested/read.txt") as handle:
        assert handle.read() == b"allowed original"
    assert replaced


def test_lazy_listing_does_not_open_or_recurse_into_files(ws, monkeypatch):
    put(ws, "work/integrations/figma/design.html", "<html>Design</html>")
    put(ws, "work/large.mp4", b"\x00\x00\x00\x18ftypisom")

    def unexpected(*args, **kwargs):
        pytest.fail("Listing must not read files")

    monkeypatch.setattr(files, "open_file", unexpected)
    entries = files.listing(ws, "work")["entries"]
    assert [entry["path"] for entry in entries] == ["work/integrations", "work/large.mp4"]
    assert entries[1]["preview_kind"] == "video" and not entries[1]["type_verified"]


def test_pagination_is_complete_and_invalidates_on_directory_change(ws):
    for name in ("Z.txt", "a.txt", "b.txt", "c.txt", "d.txt"):
        put(ws, "work/" + name)
    (ws.work / "folder").mkdir()
    paths, cursor = [], None
    while True:
        page = files.listing(ws, "work", cursor=cursor, limit=2)
        paths.extend(entry["path"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert paths == ["work/folder", "work/a.txt", "work/b.txt", "work/c.txt", "work/d.txt", "work/Z.txt"]
    old = files.listing(ws, "work", limit=2)["next_cursor"]
    put(ws, "work/new.txt")
    with pytest.raises(ValueError, match="directory changed"):
        files.listing(ws, "work", cursor=old)


def test_invalid_cursors_and_explicit_scan_cap(ws, monkeypatch):
    for i in range(6):
        put(ws, f"work/file-{i}.txt")
    for cursor in ("not json", "[]", 3, "a" * 300, base64.urlsafe_b64encode(b"{}").decode()):
        with pytest.raises(ValueError):
            files.listing(ws, "work", cursor=cursor)
    monkeypatch.setattr(files, "MAX_DIRECTORY_ENTRIES", 3)
    result = files.listing(ws, "work")
    assert result["truncated"] and result["scan_limit"] == 3
    assert len(result["entries"]) == 3 and result["notice"]


@pytest.mark.parametrize(("name", "content", "kind", "mime"), [
    ("movie.mp4", b"\x00\x00\x00\x18ftypisom\x00\x00", "video", "video/mp4"),
    ("movie.webm", b"\x1aE\xdf\xa3", "video", "video/webm"),
    ("image.png", b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    ("image.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image", "image/svg+xml"),
    ("page.html", b"<!doctype html><html><script>1</script></html>", "html", "text/html"),
    ("doc.pdf", b"%PDF-1.7", "pdf", "application/pdf"),
    ("sound.wav", b"RIFF0000WAVE", "audio", "audio/x-wav"),
    ("copy.json", b'{"title":"Example"}', "json", "application/json"),
    ("notes.md", b"# Notes", "markdown", "text/markdown"),
    ("code.py", b"print('hello')", "code", "text/plain"),
    ("HEAD", b"3", "text", "text/plain"),
    ("registry.sqlite", b"SQLite format 3\x00", "download", "application/vnd.sqlite3"),
])
def test_bounded_type_detection_and_metadata(ws, name, content, kind, mime):
    put(ws, "work/" + name, content)
    result = files.metadata(ws, "work/" + name)
    assert result["preview_kind"] == kind
    # SQLite MIME registration varies by host; no preview relies on that type.
    if not name.endswith("sqlite"):
        assert result["mime"] == mime
    assert result["size"] == len(content) and result["type_verified"]
    assert result["is_file"] and result["kind"] == "file" and len(result["etag"]) == 24
    if kind in {"text", "markdown", "code", "json"}:
        assert result["text"] == content.decode()
    else:
        assert "text" not in result
    assert str(ws.root) not in json.dumps(result)


@pytest.mark.parametrize(("name", "content"), [
    ("bad.png", b"<html>not a PNG</html>"), ("bad.pdf", b"not a PDF"),
    ("bad.mp4", b"<html>not a video</html>"), ("bad.html", b"\x00\xffbinary"),
    ("bad.txt", b"\xff\x00binary"), ("bad.svg", b"<html>not svg</html>"),
])
def test_mismatched_or_binary_content_is_not_previewed_as_declared(ws, name, content):
    put(ws, "work/" + name, content)
    result = files.metadata(ws, "work/" + name)
    assert result["preview_kind"] == "download" and result["mime"] == "application/octet-stream"
    assert result["type_warning"] and "text" not in result


def test_unicode_text_bound_discloses_exact_coverage_and_full_file_remains_readable(ws, monkeypatch):
    text = "a" * 13 + "🟠" * 10 + "tail"
    put(ws, "work/unicode.txt", text)
    monkeypatch.setattr(files, "MAX_TEXT_BYTES", 16)
    result = files.metadata(ws, "work/unicode.txt")
    assert result["text"] == "a" * 13
    assert result["text_bytes"] == 13 and result["text_limit_bytes"] == 16 and result["truncated"]
    with files.open_file(ws, "work/unicode.txt") as stream:
        assert stream.read().decode() == text


def test_binary_beyond_sniff_prefix_is_caught_in_text_preview(ws):
    put(ws, "work/binary.txt", b"a" * files.SNIFF_BYTES + b"\x00")
    result = files.metadata(ws, "work/binary.txt")
    assert result["preview_kind"] == "download" and "text" not in result


def test_metadata_read_is_bounded_for_large_files(ws, monkeypatch):
    put(ws, "work/long.txt", "x" * (files.MAX_TEXT_BYTES + 100))
    actual = files.open_file
    reads = []
    from contextlib import contextmanager

    @contextmanager
    def observed(*args, **kwargs):
        with actual(*args, **kwargs) as source:
            class Reader:
                def fileno(self):
                    return source.fileno()

                def seek(self, *args):
                    return source.seek(*args)

                def read(self, size=-1):
                    reads.append(size)
                    assert 0 <= size <= files.MAX_TEXT_BYTES
                    return source.read(size)
            yield Reader()

    monkeypatch.setattr(files, "open_file", observed)
    result = files.metadata(ws, "work/long.txt")
    assert result["truncated"] and reads == [files.SNIFF_BYTES, files.MAX_TEXT_BYTES]


def test_file_mutation_during_preview_requires_refresh(ws, monkeypatch):
    put(ws, "work/read.txt", "original")
    actual_fstat = files.os.fstat
    calls = 0

    def changing_fstat(fd):
        nonlocal calls
        calls += 1
        # _open regular-file check, metadata initial snapshot, final snapshot.
        if calls == 3:
            (ws.work / "read.txt").write_text("changed content")
        return actual_fstat(fd)

    monkeypatch.setattr(files.os, "fstat", changing_fstat)
    with pytest.raises(ValueError, match="file changed"):
        files.metadata(ws, "work/read.txt")


def test_html_sibling_assets_resolve_only_through_the_public_tree(ws):
    page = put(ws, "work/integrations/figma/page.html", '<img src="assets/hero.png"><link href="style.css">')
    put(ws, "work/integrations/figma/assets/hero.png", b"\x89PNG\r\n\x1a\n")
    put(ws, "work/integrations/figma/style.css", "body { color: black }")
    base = "http://127.0.0.1:8765/preview/r-files/key/" + str(page.relative_to(ws.root))
    for href in ("assets/hero.png", "style.css"):
        relative = unquote(urlsplit(urljoin(base, href)).path).split("/", 4)[4]
        assert files.metadata(ws, relative)["preview_kind"] in {"image", "code"}
    for href in ("../../../memory/lead_session.json", "../../../.browser/profile/Cookies"):
        relative = unquote(urlsplit(urljoin(base, href)).path).split("/", 4)[4]
        with pytest.raises(PermissionError):
            files.metadata(ws, relative)


def test_missing_file_error_has_no_host_path(ws):
    with pytest.raises(FileNotFoundError) as error:
        files.metadata(ws, "work/missing.txt")
    assert str(ws.root) not in str(error.value)
