"""Local creative apps require human opt-in without blocking read-only inspection."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    setup = r"""
import assert from 'node:assert/strict';
let cursor=0,hooks=[],effects=[],pendingEffects=[],view,props;
globalThis.window={React:{
 createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
 useState(initial){const at=cursor++;if(!(at in hooks))hooks[at]=typeof initial==='function'?initial():initial;
  return[hooks[at],value=>{hooks[at]=typeof value==='function'?value(hooks[at]):value}]},
 useRef(initial){const at=cursor++;return hooks[at]??={current:initial}},
 useEffect(callback,deps){const at=cursor++,previous=effects[at];if(!previous||deps.some((value,i)=>value!==previous.deps[i])){
  pendingEffects.push(()=>{previous?.cleanup?.();effects[at]={deps,cleanup:callback()}})}}
 },Eeva:{Button:'Button',Icon:'Icon',Tooltip:'Tooltip',MessageBar:'MessageBar'}};
const {CreativeAppConnection,creativeConnection,creativeActivity,integrationState}=await import('../../src/vwb/web/static/integrations-control.js');
function render(){cursor=0;pendingEffects=[];view=CreativeAppConnection(props);for(const effect of pendingEffects)effect();return view}
function nodes(node){if(!arguments.length)node=view;return node&&typeof node==='object'?[node,...(node.children||[]).flatMap(nodes)]:[]}
function text(node){if(!arguments.length)node=view;if(node==null||typeof node==='boolean')return '';return typeof node==='object'?(node.children||[]).map(text).join(''):String(node)}
function button(label){return nodes().find(node=>['button','Button'].includes(node.type)&&text(node)===label)}
function checks(){return nodes().filter(node=>node.type==='input'&&node.props.type==='checkbox')}
const actions=[];
const onAction=(action,body)=>actions.push({action,body});
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_after_effects_enable_requires_explicit_host_permission_and_can_be_revoked():
    run_js(r"""
props={item:{id:'after_effects',name:'After Effects',state:'disabled',enabled:false,supported:true},busy:false,onAction};
render();render();
assert.ok(creativeConnection(props.item));
assert.equal(integrationState(props.item),'Disabled');
assert.ok(text().includes('run under your account'));
assert.equal(checks()[0].props.checked,false);
assert.equal(button('Enable automation').props.disabled,true);
button('Check installation').props.onClick();
assert.deepEqual(actions,[{action:'after-effects/refresh',body:{}}]);
checks()[0].props.onChange({target:{checked:true}});render();
assert.equal(button('Enable automation').props.disabled,false);
button('Enable automation').props.onClick();
assert.deepEqual(actions.at(-1),{action:'after-effects/configure',body:{enabled:true}});
props={...props,item:{...props.item,enabled:true,state:'ready',connected:true}};render();render();
assert.equal(button('Enable automation'),undefined);
button('Disable automation').props.onClick();
assert.deepEqual(actions.at(-1),{action:'after-effects/configure',body:{enabled:false}});
props={...props,item:{...props.item,enabled:false,state:'disabled',connected:false}};render();render();
assert.equal(checks()[0].props.checked,false,'revocation must clear the earlier opt-in');
assert.equal(button('Enable automation').props.disabled,true);
props={...props,item:{...props.item,supported:false}};render();
assert.equal(checks()[0].props.disabled,true,'unsupported hosts cannot opt in');
""")


def test_blender_connect_does_not_implicitly_allow_python_execution():
    run_js(r"""
props={item:{id:'blender_mcp',name:'Blender MCP',state:'disconnected',prepared:false,allow_code_execution:false},busy:false,onAction};
render();render();
assert.ok(creativeConnection(props.item));
assert.equal(button('Connect Blender').props.disabled,true);
assert.equal(button('Enable Python execution').props.disabled,true);
button('Prepare Blender add-on').props.onClick();
assert.deepEqual(actions.at(-1),{action:'blender-mcp/prepare',body:{}});
props={...props,item:{...props.item,prepared:true,addon_path:'/host/private/addon.py'}};render();
assert.equal(button('Prepare Blender add-on'),undefined);
assert.equal(button('Connect Blender').props.disabled,false);
assert.ok(text().includes('/host/private/addon.py'));
button('Connect Blender').props.onClick();
assert.deepEqual(actions.at(-1),{action:'blender-mcp/connect',body:{}});
assert.equal(button('Enable Python execution').props.disabled,true);
checks()[0].props.onChange({target:{checked:true}});render();
button('Enable Python execution').props.onClick();
assert.deepEqual(actions.at(-1),{action:'blender-mcp/configure',body:{allow_code_execution:true}});
props={...props,item:{...props.item,allow_code_execution:true,connected:true,state:'connected'}};render();render();
button('Disable Python execution').props.onClick();
assert.deepEqual(actions.at(-1),{action:'blender-mcp/configure',body:{allow_code_execution:false}});
button('Disconnect Blender').props.onClick();
assert.deepEqual(actions.at(-1),{action:'blender-mcp/disconnect',body:{}});
assert.equal(creativeActivity('blender-mcp/prepare'),'Preparing the Blender add-on');
assert.equal(creativeActivity('after-effects/refresh'),'Checking After Effects installation');
""")


def test_interrupted_after_effects_script_requires_separate_verified_stop():
    run_js(r"""
props={item:{id:'after_effects',name:'After Effects',state:'ready',enabled:true,
 pending_execution:{job_id:'job-one',status:'interrupted',uncertain:true}},busy:false,onAction};
render();render();
assert.ok(text().includes('may still be running'));
assert.equal(button('Clear interrupted execution').props.disabled,true);
checks()[0].props.onChange({target:{checked:true}});render();
button('Clear interrupted execution').props.onClick();
assert.deepEqual(actions.at(-1),{action:'after-effects/configure',body:{acknowledge_interrupted:true}});
props={...props,busy:true};render();
assert.equal(button('Clear interrupted execution').props.disabled,true);
assert.equal(button('Disable automation').props.disabled,true);
assert.equal(checks()[0].props.disabled,true);
props={...props,busy:false,item:{...props.item,pending_execution:{job_id:'job-two',uncertain:true}}};render();render();
assert.equal(button('Clear interrupted execution').props.disabled,true,'a different script needs its own confirmation');
props={...props,item:{...props.item,pending_execution:{job_id:'job-two',uncertain:false}}};render();render();
assert.equal(button('Clear interrupted execution'),undefined,'a known running script cannot be acknowledged away');
assert.ok(text().includes('Wait for completion'));
""")
