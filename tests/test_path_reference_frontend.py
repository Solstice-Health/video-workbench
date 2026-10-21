"""Path selection and asynchronous local import behavior without model calls."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source], cwd=ROOT / "generators/remotion",
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_path_picker_preserves_spaces_and_serializes_large_batches_as_references():
    run_js(r"""
import assert from 'node:assert/strict';
const {triggerAt,matchingOptions,toContext}=await import('../../src/vwb/web/static/composer-picker.js');
for (const value of ['email@example.com', 'https://example.com/', 'Normal text', '@one and more']) {
 assert.equal(triggerAt(value),null,value);
}
assert.deepEqual(triggerAt('Reformat @"work/Campaign assets"'),{
 kind:'path',query:'work/Campaign assets',start:9,end:32});
assert.equal(triggerAt('Use @"work/Campaign assets').query,'work/Campaign assets');
const middle='Use @"work/Campaign assets" please';
const trigger=triggerAt(middle,26);
assert.equal(middle.slice(trigger.start,trigger.end),'@"work/Campaign assets"');
assert.equal(triggerAt('Use /features remake').kind,'skill');
const catalog={folders:[{id:'Campaign assets',name:'work/Campaign assets/',description:'Folder'}],
 files:Array.from({length:50},(_,i)=>({id:`Campaign assets/image ${i}.png`,name:`work/Campaign assets/image ${i}.png`,description:'Image'}))};
const options=matchingOptions(catalog,triggerAt('@"work/Campaign assets"'));
assert.equal(options.length,51); assert.equal(options[0].kind,'folder');
assert.equal(options[1].kind,'file');
assert.equal(matchingOptions(catalog,triggerAt('@"work/Campaign assets"'),[options[0]]).length,50);
const files=toContext(options.slice(1));
assert.equal(files.files.length,50);
assert.ok(!JSON.stringify(files).includes('base64'));
assert.deepEqual(toContext([options[0]]),{skills:[],folders:['Campaign assets'],plugins:[]});
assert.equal(matchingOptions({...catalog,search_query:'Campaign assets'},triggerAt('@/some/work/Campaign')).length,51);
assert.equal(matchingOptions({folders:[{id:'workplace',name:'work/workplace/'}]},triggerAt('@workplace')).length,1);
""")


def test_sidebar_drag_is_bound_to_current_workspace_and_accepts_files_or_folders():
    run_js(r"""
import assert from 'node:assert/strict';
const {workspaceReference}=await import('../../src/vwb/web/static/composer-picker.js');
const input={requestId:'r-current',path:'work/Campaign assets',kind:'directory',name:'Campaign assets'};
assert.deepEqual(workspaceReference(JSON.stringify(input),'r-current'),{
 id:'Campaign assets',kind:'folder',name:'work/Campaign assets/',description:'All files in this folder, read as needed'});
assert.equal(workspaceReference({...input,path:'work/Campaign assets/image 1.png',kind:'file'},'r-current').kind,'file');
assert.equal(workspaceReference({...input,path:'work'},'r-current').id,'.');
for(const path of ['/tmp/assets','media/source.mp4','work/../secret','work/.env','work/hidden/.env',
 'work/folder\\secret','work/node_modules/package','work/folder//file','work/a\0b']) {
 assert.equal(workspaceReference({...input,path},'r-current'),null,path);
}
assert.equal(workspaceReference(input,'r-other'),null);
assert.equal(workspaceReference(input,null),null);
assert.equal(workspaceReference('not json','r-current'),null);
""")


def test_import_labels_hide_internal_ids_without_changing_reference_identity():
    run_js(r"""
import assert from 'node:assert/strict';
const {displayPath,toContext}=await import('../../src/vwb/web/static/composer-picker.js');
const first='work/imports/0123456789abcdef0123456789abcdef/Campaign assets/';
const second='work/imports/abcdef0123456789abcdef0123456789/Campaign assets/';
assert.equal(displayPath(first),'Campaign assets/');
assert.equal(displayPath(second),'Campaign assets/');
assert.equal(displayPath(`${first}Portrait/Shot49.png`),'Campaign assets/Portrait/Shot49.png');
for(const path of ['work/outputs/short 1/','work/imports/manual/folder/','work/imports/0123456789abcdef0123456789abcdef']) {
 assert.equal(displayPath(path),path);
}
const selected=[first,second].map(name=>({kind:'folder',id:name.slice(5,-1),name}));
assert.notEqual(selected[0].name,selected[1].name); // Canonical tooltip / remove label stays unambiguous.
assert.equal(toContext(selected).folders.length,2);
assert.notEqual(...toContext(selected).folders);
""")


def test_local_import_waits_for_ready_cancels_and_recovers_inflight_work():
    run_js(r"""
import assert from 'node:assert/strict';
let cursor=0,hooks=[],effects=[],queuedEffects=[],timers=[],view;
globalThis.window={React:{
 createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
 useState(initial){const at=cursor++; if(!(at in hooks))hooks[at]=typeof initial==='function'?initial():initial;
  return [hooks[at],value=>{hooks[at]=typeof value==='function'?value(hooks[at]):value;}];},
 useRef(initial){const at=cursor++;return hooks[at]??={current:initial};},
 useEffect(callback,deps){const at=cursor++,prior=effects[at];if(!prior||deps.some((value,i)=>value!==prior.deps[i])){
   queuedEffects.push(()=>{prior?.cleanup?.();effects[at]={deps,cleanup:callback()};});}}
},Eeva:{Button:'Button',TextField:'TextField',MessageBar:'MessageBar'}};
globalThis.ReactDOM={createPortal:node=>node};
globalThis.document={body:{},activeElement:{},addEventListener(){},removeEventListener(){}};
globalThis.setTimeout=(callback,ms)=>{const timer={callback,ms,active:true};timers.push(timer);return timer;};
globalThis.clearTimeout=timer=>{if(timer)timer.active=false;};
const {LocalImportDialog,localPath,importPending}=await import('../../src/vwb/web/static/local-import-control.js');
assert.equal(localPath('"~/Downloads/Campaign assets"'),'~/Downloads/Campaign assets');
assert.equal(localPath('relative/folder'),'');
assert.equal(importPending({status:'ready'}),false);
let created=0, ready=[],pending=[],calls=[],closed=0,gets=0;
const reference={kind:'folder',id:'imports/test/Campaign assets',name:'work/imports/test/Campaign assets/'};
const api=async(url,options={})=>{
 calls.push({url,method:options.method||'GET'});
 if(options.method==='POST')return {import:{id:'test',status:'queued',files:0}};
 if(options.method==='DELETE')return {import:{id:'test',status:'cancelled',files:3}};
 gets++;
 return gets===1?{import:{id:'test',status:'running',files:3,total_files:50}}:
  {import:{id:'recovered',status:'ready',files:50,total_files:50,reference}};
};
let props={open:false,api,initialPath:'~/Downloads/Campaign assets',
 ensureWorkspace:async()=>{created++;return 'r-workspace';},
 onReady:(...args)=>ready.push(args),onPending:value=>pending.push(value),onClose:()=>closed++};
function render(){cursor=0;queuedEffects=[];view=LocalImportDialog(props);for(const effect of queuedEffects)effect();return view;}
function nodes(node=view){return node&&typeof node==='object'?[node,...(node.children||[]).flatMap(nodes)]:[];}
function button(label){return nodes().find(node=>node.type==='Button'&&node.children.includes(label));}
async function tick(){const timer=timers.findLast(timer=>timer.active);assert.equal(timer.ms,1000);timer.active=false;await timer.callback();render();}
render();props={...props,open:true};render();render();
assert.equal(nodes().find(node=>node.type==='TextField').props.value,'~/Downloads/Campaign assets');
await button('Import path').props.onClick();render();
assert.equal(created,1);assert.equal(ready.length,0);assert.equal(pending.at(-1),true);
assert.equal(button('Import path').props.disabled,true);
await tick();assert.equal(ready.length,0);
await button('Cancel import').props.onClick();render();
assert.equal(pending.at(-1),false);assert.equal(ready.length,0);
assert.equal(calls.filter(call=>call.method==='DELETE').length,1);
// A page reload opens the stored pending job. It must not start a second import.
props={...props,open:false};render();
props={...props,open:true,initialJob:{requestId:'r-workspace',row:{id:'recovered',status:'running',files:3,total_files:50}}};
render();render();
await tick();render();
assert.equal(ready.length,1);assert.deepEqual(ready[0].slice(0,2),[reference,'r-workspace']);
assert.equal(pending.at(-1),false);assert.equal(created,1);
assert.equal(calls.filter(call=>call.method==='POST').length,1);
assert.ok(calls.every(call=>call.url.includes('/imports')));
""")
