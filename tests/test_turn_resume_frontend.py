"""Recovery controls keep continuing a turn separate from releasing its queue."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    setup = """
import assert from 'node:assert/strict';
globalThis.React={Component:class{},createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)})};
globalThis.window={React,Eeva:{}};
globalThis.ReactDOM={createRoot:()=>({render(){}})};
globalThis.document={getElementById:()=>({})};
const {turnRecovery,RecoveryActions}=await import('../../src/vwb/web/static/app.js');
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_interrupted_turn_can_resume_without_queue_but_never_during_active_turn():
    run_js(r"""
const job={id:'interrupted',kind:'message',status:'failed',error:'Server restarted'};
assert.deepEqual(turnRecovery({resumable_job:job}),{job,queueCount:0});
const stopped={...job,status:'stopped'};
assert.equal(turnRecovery({resumable_job:stopped}).job,stopped);
const compact={...job,kind:'compact'};
assert.equal(turnRecovery({resumable_job:compact}).job,compact);
const cancelledContinuation={...job,id:'continuation',status:'cancelled',resume_of:job.id};
assert.equal(turnRecovery({resumable_job:cancelledContinuation}).job,cancelledContinuation);
assert.equal(turnRecovery({resumable_job:{...cancelledContinuation,resume_of:undefined}}).job,null);
const queue=[{status:'queued'},{status:'pending'},{status:'failed'}];
assert.deepEqual(turnRecovery({paused:true,queue,resumable_job:job}),{job,queueCount:2});
assert.equal(turnRecovery({paused:false,queue}).queueCount,0);
for(const active of [{kind:'message',status:'running'},{kind:'external',can_stop:false}]) {
  assert.deepEqual(turnRecovery({active,paused:true,queue,resumable_job:job}),{job:null,queueCount:0});
}
for(const changed of [{id:null},{kind:'unknown'},{status:'completed'},{status:'running'}]) {
  assert.equal(turnRecovery({resumable_job:{...job,...changed}}).job,null);
}
assert.deepEqual(turnRecovery(null),{job:null,queueCount:0});
assert.equal(turnRecovery({last_job:job}).job,null); // Backend eligibility is authoritative.
""")


def test_recovery_buttons_send_distinct_payloads_and_disable_while_saving():
    run_js(r"""
const recovery={job:{id:'saved-job',kind:'message',status:'failed'},queueCount:2};
const bodies=[];
const view=RecoveryActions({recovery,busy:false,onResume:body=>bodies.push(body)});
const buttons=view.children.filter(node=>node?.type==='button');
assert.deepEqual(buttons.map(button=>button.children.join('')),['Resume turn','Resume queued messages']);
buttons.forEach(button=>button.props.onClick());
assert.deepEqual(bodies,[{job_id:'saved-job',use_current_model:true,use_current_prompt:true},{}]);
assert.equal(buttons[0].props.title,'Continue using the selected model and prompt');
assert.ok(buttons.every(button=>button.props.disabled===false));
const saving=RecoveryActions({recovery,busy:true,onResume(){}});
assert.ok(saving.children.filter(node=>node?.type==='button').every(button=>button.props.disabled===true));
const idle=RecoveryActions({recovery:{job:recovery.job,queueCount:0},busy:false,onResume(){}});
assert.equal(idle.children.filter(node=>node?.type==='button').length,1);
""")
