"""Selection bounds and serialized writes can be checked without a browser."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or ("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node"
                               if os.path.isfile("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node") else None)
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    result = subprocess.run([NODE, "--input-type=module", "-e", source],
                            cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_selection_counts_utf16_offsets_and_line_end_boundaries():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window = {React:{createElement(){}}, Eeva:{}};
const {selectionLines}=await import('../../src/vwb/web/static/workspace-canvas.js');
assert.deepEqual(selectionLines('one\ntwo\nthree\n',0,4),{start:1,end:1});
assert.deepEqual(selectionLines('one\ntwo\nthree\n',4,9),{start:2,end:3});
assert.deepEqual(selectionLines('😀\nthird',3,8),{start:2,end:2});
assert.equal(selectionLines('short',0,0),null);
assert.equal(selectionLines('short',0,99),null);
assert.equal(selectionLines('x\n'.repeat(2001),0,4002),null);
""")


def test_slow_old_tab_cannot_overwrite_new_context_or_hidden_canvas_clear():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window = {React:{createElement(){}}, Eeva:{}};
const {queueEditorContext}=await import('../../src/vwb/web/static/workspace-canvas.js');
const calls=[]; let release;
const api=async (url,options)=>{calls.push(JSON.parse(options.body));if(calls.length===1)await new Promise(resolve=>release=resolve);};
const pending=queueEditorContext(api,'r-test',{path:'work/old.py'});
queueEditorContext(api,'r-test',{path:'work/new.py'});
queueEditorContext(api,'r-test',{path:null});
assert.deepEqual(calls,[{path:'work/old.py'}]);
release(); await pending;
assert.deepEqual(calls,[{path:'work/old.py'},{path:null}]);
await queueEditorContext(api,'r-test',{path:'work/reopened.py'});
assert.equal(calls.at(-1).path,'work/reopened.py');
""")
