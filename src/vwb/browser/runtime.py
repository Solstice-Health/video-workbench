"""Pinned native browser runtime; installation is always an explicit host action."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import stat
import tempfile
import urllib.request
from functools import lru_cache
from pathlib import Path

VERSION = "0.39.1"
RELEASE = f"https://github.com/vercel-labs/agent-browser/releases/download/v{VERSION}"
SHA256 = {
    "darwin-arm64": "cc18fac41d9dc9821918340a136e643286ea05544c4dd35671b796f1a874cdef",
    "darwin-x64": "339cb9aa6dfd404856cf938df34cd3b4779c61dfd57fc257be11b14fd1a885f9",
    "linux-arm64": "9d8fdc586056df65747927ae166157c83af91b52cea287175e5544afd2591d33",
    "linux-x64": "6906d3a2a67b05809932a93d99e53d1132373f0a52a617b2d6032dbb954df6fc",
    "linux-musl-arm64": "c4959f3e193c5bc8f333613363f25f2eaceecafe25a0821843b271906474a20f",
    "linux-musl-x64": "74507cdab98aa91bdc14346c545aff974640a225e43f8c3d6c23164f8c0f9bb9",
}
INSTALL_HINT = "Run uv run python -m vwb.browser.runtime install on the control-plane host."


def platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    libc = "-musl" if system == "linux" and platform.libc_ver()[0] == "musl" else ""
    key = f"{system}{libc}-{arch}"
    if key not in SHA256:
        raise RuntimeError("The pinned browser runtime supports macOS and Linux on arm64 or x64.")
    return key


def runtime_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "runs" / ".browser-runtime"


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@lru_cache(maxsize=32)
def _digest_for_stat(path: str, inode: int, size: int, mtime_ns: int, ctime_ns: int) -> str:
    # The identity arguments invalidate the cache on replace or in-place edits.
    return digest(Path(path))


def binary_path(directory: Path | None = None, *, verify: bool = True) -> Path:
    path = (directory or runtime_dir()) / "agent-browser"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"The pinned browser runtime is not installed. {INSTALL_HINT}")
    info = path.stat()
    identity = (str(path.resolve()), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if verify and _digest_for_stat(*identity) != SHA256[platform_key()]:
        raise RuntimeError(f"The browser runtime checksum does not match v{VERSION}. {INSTALL_HINT}")
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"The browser runtime is not executable. {INSTALL_HINT}")
    return path.resolve()


def status() -> dict:
    """Read-only installation status; never starts a process or downloads."""
    try:
        binary_path()
    except (OSError, RuntimeError) as exc:
        return {"installed": False, "version": VERSION, "error": str(exc)}
    return {"installed": True, "version": VERSION}


def private_directory(path: Path) -> Path:
    """Create a host-owned private directory, rejecting a substituted symlink."""
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("Browser private directories cannot be symbolic links")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Browser private directory has an unexpected owner")
    path.chmod(0o700)
    return path.resolve()


def socket_directory(private_dir: Path, session_name: str) -> Path:
    # Darwin limits Unix-domain socket paths to about 104 bytes. Request paths
    # can be much longer, so the endpoint lives under a short, uid-owned root.
    base = private_directory(Path("/tmp") / f"vwb-browser-{os.getuid()}")
    key = hashlib.sha256(f"{private_dir.resolve()}\0{session_name}".encode()).hexdigest()[:20]
    return private_directory(base / key)


def install(directory: Path | None = None) -> Path:
    """Download only the named native binary and verify before atomic placement."""
    key = platform_key()
    target_dir = private_directory(directory or runtime_dir())
    target = target_dir / "agent-browser"
    fd, temp_name = tempfile.mkstemp(prefix="download-", dir=target_dir)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(
            f"{RELEASE}/agent-browser-{key}", timeout=60
        ) as response:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 200 * 1024 * 1024:
                    raise RuntimeError("Browser download exceeded the expected size limit")
                output.write(chunk)
        if digest(temp) != SHA256[key]:
            raise RuntimeError("Browser download checksum did not match the pinned release")
        temp.chmod(0o700)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return binary_path(target_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "status"])
    args = parser.parse_args()
    if args.command == "install":
        print(f"Installed agent-browser {VERSION}: {install()}")
    else:
        import json
        print(json.dumps(status()))


if __name__ == "__main__":
    main()
