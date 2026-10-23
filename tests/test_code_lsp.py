"""Protocol regression tests, with opt-in tests for the actual pinned servers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from vwb.code.lsp import LspError, LspPool, from_lsp_position, metadata_manifest, to_lsp_position

PYTHON_FILES = {
    "pyproject.toml": '[project]\nname = "navigation-fixture"\nversion = "0.0.1"\n',
    "library.py": 'def greet(name: str) -> str:\n    return "Hello " + name\n',
    "main.py": 'from library import greet\n\nprint(greet("world"))\n',
}
TYPESCRIPT_FILES = {
    "tsconfig.json": json.dumps({"compilerOptions": {"strict": True, "target": "ES2022",
                                                     "jsx": "preserve", "module": "commonjs"},
                                  "include": ["*.ts", "*.tsx"]}),
    "library.ts": 'export function greet(name: string): string { return "Hello " + name; }\n',
    "main.ts": 'import { greet } from "./library";\nconsole.log(greet("world"));\n',
    "panel.tsx": 'import { greet } from "./library";\nexport const Panel = () => <div>{greet("panel")}</div>;\n',
}

FAKE_SERVER = r'''
import json, os, sys, time
from pathlib import Path
log = Path(sys.argv[1])
mode = sys.argv[2]
opened = {}
def send(message):
    packet = json.dumps(dict(jsonrpc="2.0", **message), ensure_ascii=False).encode()
    wire = ("Content-Length: %s\r\n\r\n" % len(packet)).encode() + packet
    # Deliberately split both headers and UTF-8 bodies across writes.
    for offset in range(0, len(wire), 19):
        os.write(1, wire[offset:offset + 19])
def region(line=0, column=0):
    return dict(start=dict(line=line, character=column), end=dict(line=line, character=column+5))
def diagnostics(uri, version):
    if mode == 'no_diagnostics':
        return
    if mode == 'old_diagnostics':
        send(dict(method="textDocument/publishDiagnostics", params=dict(uri=uri, version=version-1,
                                                                      diagnostics=[dict(message="stale", range=region())])))
    params = dict(uri=uri, diagnostics=[] if 'fixed' in opened[uri]['text'] else [dict(message="Type mismatch", severity=1,
                                                                                  code=123, source="fixture", range=region())])
    if mode != 'unversioned':
        params['version'] = version
    send(dict(method="textDocument/publishDiagnostics", params=params))
while True:
    length = None
    while True:
        header = sys.stdin.buffer.readline()
        if not header:
            sys.exit()
        if header in (b'\r\n', b'\n'):
            break
        if header.lower().startswith(b'content-length:'):
            length = int(header.split(b':', 1)[1])
    body = sys.stdin.buffer.read(length)
    msg = json.loads(body)
    method = msg.get('method')
    params = msg.get('params') or {}
    with log.open('a') as output:
        output.write(json.dumps(msg) + '\n')
    if not method:
        continue
    if method == 'textDocument/didOpen':
        document = params['textDocument']
        opened[document['uri']] = document
        diagnostics(document['uri'], document['version'])
    elif method == 'textDocument/didChange':
        document = params['textDocument']
        opened[document['uri']]['text'] = params['contentChanges'][0]['text']
        opened[document['uri']]['version'] = document['version']
        diagnostics(document['uri'], document['version'])
    elif method == 'textDocument/didClose':
        opened.pop(params['textDocument']['uri'], None)
    elif method == 'exit':
        break
    if 'id' not in msg:
        continue
    if method == 'initialize':
        if mode == 'bad_header':
            os.write(1, b'Content-Length: 999999999\r\n\r\n')
            continue
        if mode == 'wrong_id':
            send(dict(id=999, result=dict(bogus=True)))
        caps = dict(definitionProvider=True, referencesProvider=True, hoverProvider=True,
                    documentSymbolProvider=True, workspaceSymbolProvider=True,
                    implementationProvider=True, callHierarchyProvider=True)
        if mode == 'unsupported':
            caps = {}
        send(dict(id=msg['id'], result=dict(capabilities=caps)))
        if mode == 'server_requests':
            send(dict(id=50, method='workspace/configuration', params=dict(items=[dict(section='python.analysis')])))
            send(dict(id=51, method='workspace/applyEdit', params=dict(edit=dict(changes={}))))
            send(dict(id=52, method='window/workDoneProgress/create', params=dict(token='fixture')))
        continue
    if method == 'shutdown':
        send(dict(id=msg['id'], result=None))
        continue
    if mode == 'hang':
        time.sleep(60)
    uri = params.get('textDocument', {}).get('uri')
    if method == 'textDocument/prepareCallHierarchy':
        result = [dict(name='greet', kind=12, uri=uri, range=region(), selectionRange=region())]
    elif method in ('callHierarchy/incomingCalls', 'callHierarchy/outgoingCalls'):
        result = [dict(**{'from' if method.endswith('incomingCalls') else 'to': params['item']}, fromRanges=[region()])]
    elif method in ('textDocument/definition', 'textDocument/references', 'textDocument/implementation'):
        target = uri.rsplit('/', 1)[0] + '/library.py'
        result = [dict(uri=target, range=region()), dict(uri='file:///private/key.py', range=region())]
    elif method == 'textDocument/hover':
        result = dict(contents=dict(kind='markdown', value='greet(name: str) → str\nfile:///private/key.py'), range=region())
    elif method == 'textDocument/documentSymbol':
        result = [dict(name='greet', kind=12, range=region(), selectionRange=region(), children=[dict(name='name', kind=13,
                                                                                                 range=region(), selectionRange=region())])]
    elif method == 'workspace/symbol':
        result = [dict(name=params['query'], kind=12, location=dict(uri=next(iter(opened)), range=region()))]
    else:
        send(dict(id=msg['id'], error=dict(code=-32601, message='No such method file:///private/key.py')))
        continue
    send(dict(id=msg['id'], result=result))
'''


@pytest.fixture
def source(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "main.py").write_text('from library import greet\nprint(greet("world"))\n')
    (work / "library.py").write_text('def greet(name: str):\n    return name\n')
    return work


@pytest.fixture
def pool_factory(source, tmp_path):
    pools = []

    def create(mode="normal"):
        script = tmp_path / "server.py"
        script.write_text(textwrap.dedent(FAKE_SERVER))
        log = tmp_path / f"{mode}-{len(pools)}.jsonl"
        command = (sys.executable, "-u", str(script), str(log), mode)
        pool = LspPool(source, _commands={"python": command, "typescript": command})
        pools.append(pool)
        return pool, log

    yield create
    for pool in pools:
        pool.close()


def messages(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_unicode_coordinates_are_code_points_not_utf16_and_unicode_separators_are_not_newlines():
    text = 'a😀b\u2028c\r\nx\ry\nz'
    assert to_lsp_position(text, 1, 3) == {"line": 0, "character": 3}
    assert to_lsp_position(text, 2, 2) == {"line": 1, "character": 1}
    assert from_lsp_position(text, {"line": 0, "character": 3}) == {"line": 1, "column": 3}
    # A malformed half-surrogate position clamps to a complete code point.
    assert from_lsp_position(text, {"line": 0, "character": 2}) == {"line": 1, "column": 2}
    with pytest.raises(ValueError):
        to_lsp_position(text, 1, 100)


def test_definition_correlates_responses_and_redacts_external_locations(source, pool_factory):
    pool, log = pool_factory("wrong_id")
    result = pool.call({"operation": "definition", "path": "main.py", "line": 2, "column": 7})
    assert result["status"] == "ok"
    assert result["items"][0]["path"] == "library.py"
    assert result["items"][0]["sha256"] == hashlib.sha256((source / "library.py").read_bytes()).hexdigest()
    assert result["items"][1] == {"role": "external_library", "label": "External library"}
    assert "/private" not in json.dumps(result)
    assert messages(log)[-1]["params"]["position"] == {"line": 1, "character": 6}


def test_syncs_every_open_document_and_closes_deleted_files(source, pool_factory):
    pool, log = pool_factory()
    pool.call({"operation": "hover", "path": "library.py"})
    pool.call({"operation": "hover", "path": "main.py"})
    (source / "library.py").write_text('def greet(name: str):\n    return "fixed"\n')
    result = pool.call({"operation": "hover", "path": "main.py"})
    assert result["status"] == "ok"
    rows = messages(log)
    changed = [row for row in rows if row.get("method") == "textDocument/didChange"]
    assert len(changed) == 1
    assert changed[0]["params"]["textDocument"]["uri"].endswith("/library.py")
    assert changed[0]["params"]["textDocument"]["version"] == 2
    (source / "library.py").unlink()
    pool.call({"operation": "document_symbols", "path": "main.py"})
    assert any(row.get("method") == "textDocument/didClose" for row in messages(log))


def test_stale_expected_hash_never_starts_a_server(pool_factory):
    pool, log = pool_factory()
    result = pool.call({"operation": "hover", "path": "main.py", "expected_sha256": "0" * 64})
    assert result["status"] == "stale"
    assert not log.exists()


def test_changed_unopened_dependency_restarts_server_instead_of_waiting_for_watchers(source, pool_factory):
    pool, log = pool_factory()
    # A definition result causes the server to read library.py without didOpen.
    pool.call({"operation": "definition", "path": "main.py"})
    old = next(iter(pool.servers.values())).process
    (source / "library.py").write_text('# shifted\ndef greet(name: str):\n    return name\n')
    pool.call({"operation": "definition", "path": "main.py"})
    assert old.poll() is not None
    assert sum(row.get("method") == "initialize" for row in messages(log)) == 2


def host_request(source):
    return {"operation": "lsp", "host_manifest": {"root": ".", "family": "python"},
            "host_evidence": [{"path": name, "sha256": hashlib.sha256((source / name).read_bytes()).hexdigest()}
                              for name in ("main.py", "library.py")]}


def test_host_content_baseline_keeps_warm_servers_but_invalidates_identical_stat_dependencies(source, pool_factory, monkeypatch):
    pool, log = pool_factory()
    request = host_request(source)
    assert not pool.accept_host_evidence(request)
    pool.call({"operation": "definition", "path": "main.py"})
    old = next(iter(pool.servers.values())).process
    assert not pool.accept_host_evidence(request)
    assert next(iter(pool.servers.values())).process is old
    # Simulate Docker Desktop reporting exactly the previous inode/timestamps
    # despite a same-size content edit already visible through actual reads.
    unchanged = pool._snapshot(source, "python")
    monkeypatch.setattr(pool, "_snapshot", lambda *_: unchanged)
    (source / "library.py").write_text((source / "library.py").read_text().replace("name", "word"))
    assert pool.accept_host_evidence(host_request(source))
    assert old.poll() is not None
    pool.call({"operation": "definition", "path": "main.py"})
    assert sum(row.get("method") == "initialize" for row in messages(log)) == 2


def test_host_baseline_does_not_trust_a_legacy_warm_server(source, pool_factory):
    pool, _ = pool_factory()
    pool.call({"operation": "definition", "path": "main.py"})
    old = next(iter(pool.servers.values())).process
    assert pool.accept_host_evidence(host_request(source))
    assert old.poll() is not None


def test_host_baseline_is_bounded_and_ignores_other_operations(source, pool_factory):
    pool, _ = pool_factory()
    assert not pool.accept_host_evidence({"operation": "read"})
    for number in range(6):
        (source / str(number)).mkdir()
        pool.accept_host_evidence({"operation": "lsp", "host_manifest": {"root": str(number), "family": "python"},
                                   "host_evidence": []})
    assert len(pool.host_evidence) == 4


def test_inflight_target_edit_discards_stale_ranges_instead_of_attaching_new_hash(source, pool_factory, monkeypatch):
    pool, _ = pool_factory()
    normalize = pool._normalize

    def race(*args):
        (source / "library.py").write_text('# changed during the request\ndef greet(name: str):\n    return name\n')
        return normalize(*args)

    monkeypatch.setattr(pool, "_normalize", race)
    result = pool.call({"operation": "definition", "path": "main.py"})
    assert result["status"] == "stale"
    assert result["items"] == []


def test_nested_project_detection_and_config_edits_restart_server(source, pool_factory):
    nested = source / "nested"
    nested.mkdir()
    (nested / "main.py").write_text("value = 1\n")
    (nested / "pyproject.toml").write_text('[project]\nname="nested"\n')
    pool, log = pool_factory()
    first = pool.call({"operation": "document_symbols", "path": "nested/main.py"})
    assert first["root"] == "nested"
    old = next(iter(pool.servers.values())).process
    (nested / "pyproject.toml").write_text('[project]\nname="changed"\n')
    pool.call({"operation": "document_symbols", "path": "nested/main.py"})
    assert old.poll() is not None
    assert sum(row.get("method") == "initialize" for row in messages(log)) == 2


def test_root_pool_evicts_old_root_processes(source, pool_factory):
    pool, _ = pool_factory()
    old = None
    for index in range(3):
        folder = source / f"project{index}"
        folder.mkdir()
        (folder / "pyproject.toml").write_text('[project]\nname="fixture"\n')
        (folder / "main.py").write_text("x = 1\n")
        pool.call({"operation": "hover", "path": f"project{index}/main.py"})
        if index == 0:
            old = next(iter(pool.servers.values())).process
    assert len(pool.servers) == 2
    assert old.poll() is not None


def test_document_workspace_symbols_and_hierarchy_are_normalized(pool_factory):
    pool, _ = pool_factory()
    result = pool.call({"operation": "document_symbols", "path": "main.py"})
    assert [item["name"] for item in result["items"]] == ["greet", "name"]
    assert result["items"][1]["container"] == "greet"
    result = pool.call({"operation": "workspace_symbols", "path": "main.py", "query": "specific"})
    assert result["items"][0]["name"] == "specific"
    for operation, role in (("callers", "caller"), ("callees", "callee")):
        result = pool.call({"operation": operation, "path": "main.py"})
        assert result["items"][0]["role"] == role
        assert result["items"][0]["call_sites"][0]["role"] == "call_site"
    result = pool.call({"operation": "document_symbols", "path": "main.py", "limit": 1})
    assert len(result["items"]) == 1 and result["truncated"]


def test_hover_does_not_return_server_file_uris(pool_factory):
    pool, _ = pool_factory()
    result = pool.call({"operation": "hover", "path": "main.py"})
    assert "→" in result["items"][0]["text"]
    assert "file://" not in json.dumps(result)
    assert "/private" not in json.dumps(result)


def test_server_requests_cannot_apply_edits_and_configuration_is_client_controlled(pool_factory):
    pool, log = pool_factory("server_requests")
    pool.call({"operation": "hover", "path": "main.py"})
    rows = messages(log)
    assert next(row for row in rows if row.get("id") == 51 and "result" in row)["result"]["applied"] is False
    assert next(row for row in rows if row.get("id") == 50 and "result" in row)["result"][0]["diagnosticMode"] == "openFilesOnly"


def test_diagnostics_ignore_old_versions_and_refresh_after_edit(source, pool_factory):
    pool, _ = pool_factory("old_diagnostics")
    first = pool.call({"operation": "diagnostics", "path": "main.py"})
    assert first["complete"] and first["diagnostic_version"] == 1
    assert first["items"][0]["message"] == "Type mismatch"
    (source / "main.py").write_text("fixed = 1\n")
    second = pool.call({"operation": "diagnostics", "path": "main.py"})
    assert second["complete"] and second["diagnostic_version"] == 2
    assert second["items"] == []


def test_unversioned_diagnostics_do_not_claim_complete(pool_factory):
    pool, _ = pool_factory("unversioned")
    result = pool.call({"operation": "diagnostics", "path": "main.py"})
    assert result["freshness"] == "unversioned_notification"
    assert result["complete"] is False


def test_no_diagnostics_returns_pending_not_clean(pool_factory):
    pool, _ = pool_factory("no_diagnostics")
    result = pool.call({"operation": "diagnostics", "path": "main.py", "timeout_s": 0.3})
    assert result["status"] == "pending"
    assert not result["complete"]


def test_unsupported_capability_is_explicit(pool_factory):
    pool, log = pool_factory("unsupported")
    result = pool.call({"operation": "implementation", "path": "main.py"})
    assert result["status"] == "unsupported"
    assert not any(row.get("method") == "textDocument/implementation" for row in messages(log))


@pytest.mark.parametrize("mode", ["hang", "bad_header"])
def test_timeouts_and_bad_framing_are_bounded_and_processes_reaped(pool_factory, mode):
    pool, _ = pool_factory(mode)
    start = time.monotonic()
    with pytest.raises((TimeoutError, LspError)):
        pool.call({"operation": "hover", "path": "main.py", "timeout_s": 0.4})
    assert time.monotonic() - start < 3
    assert not pool.servers


@pytest.mark.parametrize("value", ["../main.py", "/tmp/main.py", ".env.py", "link/main.py"])
def test_untrusted_paths_never_start_a_server(source, pool_factory, value):
    (source / "link").symlink_to(source, target_is_directory=True)
    pool, log = pool_factory()
    with pytest.raises(ValueError):
        pool.call({"operation": "hover", "path": value})
    assert not log.exists()


def test_production_never_runs_language_servers_on_host(source, monkeypatch):
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False if str(path) == "/.dockerenv" else original(path))
    pool = LspPool(source)
    with pytest.raises(LspError, match="Docker"):
        pool.call({"operation": "hover", "path": "main.py"})


def test_metadata_manifest_is_portable_and_never_starts_a_server(source, monkeypatch):
    def no_process(*args, **kwargs):
        raise AssertionError("Metadata discovery must not start subprocesses.")

    monkeypatch.setattr("vwb.code.lsp.subprocess.Popen", no_process)
    manifest = metadata_manifest(source)
    assert manifest["complete"]
    info = (source / "main.py").stat()
    assert manifest["files"]["main.py"] == {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def test_typescript_explicit_fixed_binary_and_no_automatic_installation(source, pool_factory):
    (source / "main.tsx").write_text("export const value: number = 1;\n")
    pool, log = pool_factory()
    pool.call({"operation": "document_symbols", "path": "main.tsx"})
    init = next(row for row in messages(log) if row.get("method") == "initialize")["params"]
    options = init["initializationOptions"]
    assert options["plugins"] == []
    assert options["disableAutomaticTypingAcquisition"] is True
    assert options["tsserver"]["path"] == "/opt/vwb/lsp/node_modules/typescript/lib/tsserver.js"
    assert options["tsserver"]["useSyntaxServer"] == "never"


@pytest.mark.skipif(os.environ.get("VWB_CODE_LSP_INTEGRATION") != "1", reason="Pinned servers run only in Docker")
@pytest.mark.parametrize("language,files,main,definition,line,column", [
    ("python", PYTHON_FILES, "main.py", "library.py", 3, 8),
    ("typescript", TYPESCRIPT_FILES, "main.ts", "library.ts", 2, 14),
    ("typescript", TYPESCRIPT_FILES, "panel.tsx", "library.ts", 2,
     TYPESCRIPT_FILES["panel.tsx"].splitlines()[1].index("greet") + 2),
])
def test_actual_pinned_servers(tmp_path, language, files, main, definition, line, column):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    with LspPool(tmp_path) as pool:
        result = pool.call({"operation": "definition", "path": main, "line": line, "column": column, "timeout_s": 30})
        assert result["status"] == "ok", result
        assert any(item.get("path") == definition for item in result["items"]), result
        assert result["language"] == language
        if language == "typescript":
            assert result["typescript"] == {"version": "6.0.3", "source": "user-setting"}
        assert pool.call({"operation": "document_symbols", "path": definition})["items"]
        assert pool.call({"operation": "hover", "path": main, "line": line, "column": column})["items"]


@pytest.mark.skipif(os.environ.get("VWB_CODE_LSP_INTEGRATION") != "1", reason="Pinned servers run only in Docker")
def test_actual_typescript_honors_nested_tsconfig_module_aliases(tmp_path):
    project = tmp_path / "nested"
    project.mkdir()
    config = {"compilerOptions": {"baseUrl": ".", "paths": {"@local/*": ["src/*"]}, "strict": True}}
    (project / "tsconfig.json").write_text(json.dumps(config))
    (project / "src").mkdir()
    (project / "src/library.ts").write_text(TYPESCRIPT_FILES["library.ts"])
    (project / "main.ts").write_text('import { greet } from "@local/library";\nconsole.log(greet("world"));\n')
    with LspPool(tmp_path) as pool:
        result = pool.call({"operation": "definition", "path": "nested/main.ts", "line": 2, "column": 14})
        assert result["root"] == "nested"
        assert any(item.get("path") == "nested/src/library.ts" for item in result["items"]), result


@pytest.mark.skipif(os.environ.get("VWB_CODE_LSP_INTEGRATION") != "1", reason="Pinned servers run only in Docker")
@pytest.mark.parametrize("files,main,library,line,column", [
    (PYTHON_FILES, "main.py", "library.py", 3, 8),
    (TYPESCRIPT_FILES, "main.ts", "library.ts", 2, 14),
])
def test_actual_servers_refresh_dependencies_never_explicitly_opened(tmp_path, files, main, library, line, column):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    with LspPool(tmp_path) as pool:
        first = pool.call({"operation": "definition", "path": main, "line": line, "column": column})
        assert any(item.get("path") == library for item in first["items"]), first
        prefix = "# new header\n# moves the declaration\n" if library.endswith(".py") else "// new header\n// moves the declaration\n"
        (tmp_path / library).write_text(prefix + files[library])
        second = pool.call({"operation": "definition", "path": main, "line": line, "column": column})
        target = next(item for item in second["items"] if item.get("path") == library)
        assert target["range"]["start"]["line"] == 3, second


@pytest.mark.skipif(os.environ.get("VWB_CODE_LSP_INTEGRATION") != "1", reason="Pinned servers run only in Docker")
@pytest.mark.parametrize("files,main,library,line,column", [
    (PYTHON_FILES, "main.py", "library.py", 3, 8),
    (TYPESCRIPT_FILES, "main.ts", "library.ts", 2, 14),
])
def test_actual_servers_synchronize_both_edited_files_and_diagnostics(tmp_path, files, main, library, line, column):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    with LspPool(tmp_path) as pool:
        assert pool.call({"operation": "document_symbols", "path": library})["items"]
        original = pool.call({"operation": "definition", "path": main, "line": line, "column": column})
        assert any(item.get("path") == library for item in original["items"])
        (tmp_path / library).write_text(files[library].replace("greet", "wave"))
        (tmp_path / main).write_text(files[main].replace("greet", "wave"))
        changed = pool.call({"operation": "definition", "path": main, "line": line, "column": column})
        assert changed["version"] == 2
        assert any(item.get("path") == library for item in changed["items"]), changed
        hover = pool.call({"operation": "hover", "path": main, "line": line, "column": column})
        assert "wave" in json.dumps(hover), hover
        stale = pool.call({"operation": "hover", "path": main, "expected_sha256": original["sha256"]})
        assert stale["status"] == "stale"
        extension = Path(main).suffix
        error = 'bad: int = "wrong"\n' if extension == ".py" else 'export const bad: number = "wrong";\n'
        (tmp_path / main).write_text(error)
        result = pool.call({"operation": "diagnostics", "path": main, "timeout_s": 30})
        assert result["status"] == "ok", result
        assert result["items"], result
        assert all(item["sha256"] == hashlib.sha256(error.encode()).hexdigest() for item in result["items"])


def test_fake_fixture_is_valid_python():
    # Keep an early assertion for accidental syntax errors in the child source.
    compile(FAKE_SERVER, "fixture.py", "exec")
