"""Every trusted browser entrypoint must ship in installed builds as well as checkouts."""
import tomllib
from pathlib import Path


def test_renderer_runtime_files_ship_in_wheel_and_source_packages():
    root = Path(__file__).resolve().parents[1]
    targets = tomllib.loads((root / 'pyproject.toml').read_text())['tool']['hatch']['build']['targets']
    for file in (root / 'generators/remotion').glob('browser-*.mjs'):
        path = file.relative_to(root).as_posix()
        assert targets['wheel']['force-include'][path] == 'vwb/_resources/' + path
        assert path in targets['sdist']['only-include']
    assert 'docs/ADAPTABLE_CANVAS.md' in targets['wheel']['force-include']
