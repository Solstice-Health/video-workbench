"""The asset registry with late binding (spec D10, D15).

Canvas files cite assets as slot:<name>@<rev>. Revisions are immutable, so a swap-in adds
a revision and only layers that cite the slot re-render. Provenance is computed from the
receipt of the tool that made the asset; register() takes no prov argument.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from vwb.util.hashing import sha256_file, short
from vwb.util.jsonio import dumps

# provenance classes, best to worst (spec "Provenance classes")
PROV_RANK = ["master", "original_pixel", "fused", "segmented", "traced", "generative_recovery",
             "generative_motion", "generated", "composite", "unknown"]

# which tool receipts yield which provenance class
RECEIPT_PROV = {
    "human.upload": "master",
    "asset_lab.rung0": "original_pixel",
    "asset_lab.rung1": "fused",
    "asset_lab.rung2": "segmented",
    "asset_lab.rung3": "generative_recovery",
    "asset_lab.rung4": "traced",
    "asset_lab.rung5": "generative_motion",
    "matte.extract": "segmented",               # source pixels kept, alpha computed
    "asset.trace": "traced",                    # re-drawn as vector curves (VTracer)
    "canvas.snapshot": "composite",             # a frame of a designed canvas (for motion between pages)
    "sunburst.extract": "generative_recovery",  # a source element, redrawn by the image model
    "sunburst.inpaint": "generative_recovery",  # source image, masked pixels regenerated
    "sunburst.outpaint": "generative_recovery", # source image, extended by the image model
    "sunburst.create": "generated",             # new pixels, not from the source
    "omni.generate": "generative_motion",       # motion made by the video model
    "omni.key": "generative_motion",            # the same clip keyed to alpha
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
  slot TEXT NOT NULL, rev INTEGER NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  sha256 TEXT NOT NULL, prov TEXT NOT NULL, receipt TEXT NOT NULL, derived_from TEXT NOT NULL,
  fit TEXT NOT NULL, notes TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (slot, rev)
);
"""


class Registry:
    def __init__(self, assets_dir: Path):
        self.dir = Path(assets_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "registry.sqlite"
        with self._conn() as con:
            con.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def register(self, path: Path, *, slot: str, kind: str, receipt: str,
                 derived_from: list[dict[str, Any]], fit: dict[str, Any] | None = None, notes: str = "") -> str:
        """Store a new immutable revision. Returns its ref, slot:<name>@<rev>."""
        prov = RECEIPT_PROV.get(receipt, "unknown")
        digest = sha256_file(path)
        with self._conn() as con:
            row = con.execute("SELECT MAX(rev) AS r FROM revisions WHERE slot=?", (slot,)).fetchone()
            rev = (row["r"] or 0) + 1
            dest = self.dir / slot / f"r{rev}_{short(digest)}{Path(path).suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            con.execute("INSERT INTO revisions (slot, rev, kind, path, sha256, prov, receipt, derived_from, fit, notes)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (slot, rev, kind, str(dest), digest, prov, receipt, dumps(derived_from, None),
                         dumps(fit or {}, None), notes))
        return f"slot:{slot}@{rev}"

    @staticmethod
    def parse_ref(ref: str) -> tuple[str, int | None]:
        body = ref.removeprefix("slot:")
        name, _, rev = body.partition("@")
        return name, (int(rev) if rev else None)

    def get(self, ref: str) -> dict[str, Any]:
        slot, rev = self.parse_ref(ref)
        with self._conn() as con:
            if rev is None:
                row = con.execute("SELECT * FROM revisions WHERE slot=? ORDER BY rev DESC LIMIT 1", (slot,)).fetchone()
            else:
                row = con.execute("SELECT * FROM revisions WHERE slot=? AND rev=?", (slot, rev)).fetchone()
        if row is None:
            raise KeyError(f"No asset {ref}")
        return dict(row)

    def latest_ref(self, slot: str) -> str | None:
        try:
            return f"slot:{slot}@{self.get('slot:' + slot)['rev']}"
        except KeyError:
            return None

    def list(self) -> list[dict[str, Any]]:
        with self._conn() as con:
            return [dict(r) for r in con.execute("SELECT * FROM revisions ORDER BY slot, rev")]
