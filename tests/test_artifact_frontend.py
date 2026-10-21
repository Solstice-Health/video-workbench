"""Pure frontend contracts run under the existing Remotion Node runtime."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    result = subprocess.run([NODE,"--input-type=module","-e",source],cwd=ROOT/"generators/remotion",capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr


def test_bridge_accepts_only_current_opaque_frame_and_valid_selection():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window={React:{createElement(){}},Eeva:{}};
const {artifactProposal}=await import('../../src/vwb/web/static/artifact-panel.js');
const frame={},artifact={id:'review',revision:2,items:[{id:'shot-1'}]};
const event={source:frame,origin:'null',data:{type:'vwb-artifact-select',artifact_id:'review',revision:2,selection:{item_id:'shot-1',time_s:1.5,region:{x:.1,y:.2,width:.4,height:.5},text:'Closer'}}};
assert.deepEqual(artifactProposal(event,frame,artifact),event.data.selection);
for(const changed of [{source:{}},{origin:'http://localhost'},{data:{...event.data,revision:1}},{data:{...event.data,artifact_id:'other'}},{data:{...event.data,type:'submit'}},{data:{...event.data,selection:{item_id:'unknown'}}},{data:{...event.data,selection:{item_id:'shot-1',time_s:Infinity}}},{data:{...event.data,selection:{item_id:'shot-1',region:{x:.9,y:0,width:.2,height:1}}}}])assert.equal(artifactProposal({...event,...changed},frame,artifact),null);
assert.equal(artifactProposal(event,null,artifact),null);
assert.equal(artifactProposal(event,frame,null),null);
""")


def test_publish_event_keeps_reviewed_revision_pinned_without_duplicating():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window={React:{createElement(){}},Eeva:{}};
const {importedTabs,canvasReducer,initialCanvas}=await import('../../src/vwb/web/static/workspace-canvas.js');
const first={seq:10,kind:'artifact.published',data:{id:'review',title:'Review',revision:1}};
assert.equal(importedTabs([first],undefined).tabs.length,0);
const tab=importedTabs([first],9).tabs[0];
let state=canvasReducer(initialCanvas(),{type:'open',tab});
const second={...first,seq:11,data:{...first.data,revision:2,title:'Review updated'}};
const newest=importedTabs([first,second],10).tabs[0];
state=canvasReducer(state,{type:'open',tab:newest});
assert.equal(state.groups[0].tabs.length,2);
assert.equal(state.groups[0].tabs.find(t=>t.id===tab.id).revision,1);
assert.equal(state.groups[0].tabs.find(t=>t.id===tab.id).latestRevision,2);
state=canvasReducer(state,{type:'open',tab:{...newest,automatic:false}});
assert.equal(state.groups[0].tabs.find(t=>t.id===tab.id).revision,2);
assert.equal(state.groups[0].tabs.find(t=>t.id===tab.id).title,'Review updated');
assert.equal(importedTabs([first,second],11).tabs.length,0);
""")
