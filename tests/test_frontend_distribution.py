"""The installed UI is complete and serves its runtime without a CDN or npm build.

This module also runs directly against an extracted/installed wheel; it needs only
the standard library and deliberately locates assets through package resources.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
from functools import cache
from html.parser import HTMLParser
from importlib.resources import files
from urllib.parse import unquote, urlsplit


@cache
def frontend_files() -> dict[str, bytes]:
    root = files("vwb.web").joinpath("static")
    result = {}

    def walk(directory, prefix=""):
        for item in directory.iterdir():
            name = f"{prefix}{item.name}"
            if item.is_dir():
                walk(item, f"{name}/")
            else:
                result[name] = item.read_bytes()

    walk(root)
    return result


class DocumentAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.references.append(attrs["src"])
        elif tag == "link" and attrs.get("href"):
            self.references.append(attrs["href"])


def resolve_asset(source: str, reference: str) -> str | None:
    reference = reference.strip()
    if reference.startswith(("data:", "#")):
        return None
    parsed = urlsplit(reference)
    assert not parsed.scheme and not parsed.netloc, f"External runtime dependency: {source} -> {reference}"
    path = unquote(parsed.path)
    resolved = posixpath.normpath(path.lstrip("/") if path.startswith("/") else posixpath.join(posixpath.dirname(source), path))
    assert resolved != ".." and not resolved.startswith("../"), f"Asset escapes package: {reference}"
    return resolved


def runtime_references(name: str, content: bytes) -> list[str]:
    source = content.decode("utf-8")
    if name.endswith(".html"):
        parser = DocumentAssets()
        parser.feed(source)
        return parser.references
    if name.endswith((".js", ".mjs")):
        # Browser ESM files have literal imports. Do not mistake user-facing prose
        # containing 'import' or URLs passed to application functions for imports.
        imports = re.findall(r"(?:^|\n)\s*(?:import|export)\s+(?:[\s\w{},*$]+?\s+from\s*)?['\"]([^'\"]+)['\"]", source)
        imports.extend(re.findall(r"\bimport\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", source))
        assert all(item.startswith(("./", "../", "/")) for item in imports), f"Bare browser import in {name}: {imports}"
        return imports
    if name.endswith(".css"):
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        urls = [next(value for value in match if value) for match in re.findall(r"url\(\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s)]+))\s*\)", source)]
        urls.extend(re.findall(r"@import\s+['\"]([^'\"]+)['\"]", source))
        return urls
    return []


def test_browser_runtime_assets_are_self_contained():
    packaged = frontend_files()
    pending, visited = ["index.html"], set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        assert name in packaged, f"Missing packaged frontend asset: {name}"
        visited.add(name)
        if name.endswith((".html", ".js", ".mjs", ".css")):
            for reference in runtime_references(name, packaged[name]):
                target = resolve_asset(name, reference)
                if target:
                    assert target in packaged, f"Broken frontend reference: {name} -> {reference}"
                    pending.append(target)
    assert {name for name in packaged if "/" not in name and name.endswith(".js")} <= visited
    assert {"vendor/react.production.min.js", "vendor/react-dom.production.min.js", "vendor/htm.mjs",
            "vendor/eeva/components/bundle.js", "vendor/eeva/fonts/spectral-300-0.ttf"} <= visited


def test_font_files_and_stylesheet_order_are_preserved():
    packaged = frontend_files()
    html = packaged["index.html"].decode()
    styles = ["/vendor/eeva/components/bundle.css", "/vendor/eeva/tokens.css", "/app.css"]
    assert [html.index(name) for name in styles] == sorted(html.index(name) for name in styles)
    for weight in (100, 300, 400, 600, 700, 800):
        assert packaged[f"vendor/eeva/fonts/aileron-{weight}.woff2"].startswith(b"wOF2")
    assert packaged["vendor/eeva/fonts/spectral-300-0.ttf"].startswith(b"\x00\x01\x00\x00")


def test_vendor_inventory_and_dependency_notices_are_preserved():
    packaged = frontend_files()
    prefix = "vendor/eeva/"
    manifest = json.loads(packaged[f"{prefix}checksums.json"])
    for name, expected in manifest.items():
        assert f"{prefix}{name}" in packaged, f"Missing vendored file: {name}"
        assert hashlib.sha256(packaged[f"{prefix}{name}"]).hexdigest() == expected, f"Vendored checksum changed: {name}"
    dependencies = json.loads(packaged[f"{prefix}bundle-dependencies.json"])
    notices = ["vendor/htm-LICENSE.txt", "vendor/react-LICENSE.txt", "vendor/react-dom-LICENSE.txt",
               f"{prefix}fonts/Spectral-OFL.txt", f"{prefix}fonts/Aileron-NOTICE.txt"]
    notices.extend(f"{prefix}licenses/{name.replace('/', '__')}.txt" for name in dependencies)
    for name in notices:
        assert len(packaged.get(name, b"")) > 100, f"Missing dependency notice: {name}"


def test_frontend_checks_require_node_when_ci_requests_them():
    if os.environ.get("VWB_REQUIRE_FRONTEND_TESTS") == "1":
        assert shutil.which("node"), "Node must be on PATH; frontend interaction tests must not silently skip in CI."


if __name__ == "__main__":
    checks = [value for name, value in globals().copy().items() if name.startswith("test_") and callable(value)]
    for check in checks:
        check()
    print(f"Frontend distribution: {len(checks)} checks passed ({len(frontend_files())} packaged files).")
