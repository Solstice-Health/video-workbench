"""Prompt choices preview exact instructions and save within their conversation."""
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
let cursor=0,hooks=[],effects=[],pendingEffects=[],props,view,Component;
const listeners=new Map(),root={inert:false};
globalThis.document={body:{},activeElement:{isConnected:true,focus(){}},getElementById:()=>root,
 addEventListener(name,fn){listeners.set(name,fn)},removeEventListener(name){listeners.delete(name)}};
globalThis.window={innerWidth:1280,innerHeight:800,addEventListener(){},removeEventListener(){},React:{
 createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
 useState(initial){const at=cursor++;if(!(at in hooks))hooks[at]=typeof initial==='function'?initial():initial;
  return[hooks[at],value=>{hooks[at]=typeof value==='function'?value(hooks[at]):value}]},
 useRef(initial){const at=cursor++;return hooks[at]??={current:initial}},
 useId(){const at=cursor++;return hooks[at]??=`id-${at}`},
 useEffect(callback,deps){const at=cursor++,previous=effects[at];if(!previous||deps.some((value,i)=>value!==previous.deps[i])){
  pendingEffects.push(()=>{previous?.cleanup?.();effects[at]={deps,cleanup:callback()}})}}
 },Eeva:{Button:'Button',Icon:'Icon',Tooltip:'Tooltip',TextField:'TextField',Checkbox:'Checkbox'}};
window.ReactDOM={createPortal:node=>node};
const {PromptProfiles,ModelControl,ManagerDialog}=await import('../../src/vwb/web/static/model-control.js');
function render(){cursor=0;pendingEffects=[];view=Component(props);for(const effect of pendingEffects)effect();return view}
function nodes(node){if(!arguments.length)node=view;return node&&typeof node==='object'?[node,...(node.children||[]).flatMap(nodes)]:[]}
function text(node){if(!arguments.length)node=view;if(node==null||typeof node==='boolean')return '';return typeof node==='object'?(node.children||[]).map(text).join(''):String(node)}
function button(label){return nodes().find(node=>['button','Button'].includes(node.type)&&text(node)===label)}
async function settle(){await new Promise(resolve=>setImmediate(resolve));render();render()}
const profiles=[
 {id:'descriptive',label:'Descriptive',description:'Detailed Workbench guidance',kind:'workbench',sources:[],content:'SHARED CORE\nFull guidance'},
 {id:'minimal',label:'Minimal',description:'Only essential guidance',kind:'workbench',sources:[],content:'SHARED CORE\nDo the task'},
 {id:'cursor_inspired',label:'Cursor inspired',description:'Plan, navigate and verify',kind:'inspired',sources:[
  {title:'Official guidance',url:'https://cursor.com/docs/agent'},{title:'Unsafe',url:'javascript:alert(1)'}],content:'SHARED CORE\n<literal source>\nNo HTML interpretation'}];
const promptCatalog={profiles,default_profile_id:'descriptive',selection_note:'Applies to new and resumed turns. Active turns and queued messages keep their captured prompt.'};
const catalog={models:[{id:'opus',label:'Opus',provider:'anthropic',available:true,enabled:true,capabilities:{contexts:[1000000],efforts:['max']}}],
 providers:[{id:'anthropic',label:'Anthropic'}],default_selection:{model_id:'opus',context_tokens:1000000,effort:'max'},enabled_model_ids:['opus']};
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_prompt_browsing_previews_literal_content_without_saving_until_selected():
    run_js(r"""
const selected=[];
Component=PromptProfiles;props={catalog:promptCatalog,selection:{profile_id:'descriptive',active_profile:profiles[1]},busy:false,onSelect:id=>selected.push(id)};
render();render();assert.ok(text().includes('The current turn uses Minimal'));
assert.equal(button('DescriptiveSelected prompt').props['aria-pressed'],true);
button('Cursor inspired').props.onClick();render();
assert.deepEqual(selected,[]);
assert.equal(nodes().find(node=>node.type==='pre').children[0],profiles[2].content);
assert.ok(text().includes('Adapted for Workbench from public guidance'));
const links=nodes().filter(node=>node.type==='a');assert.equal(links.length,1);
assert.equal(links[0].props.href,'https://cursor.com/docs/agent');
button('Use Cursor inspired').props.onClick();assert.deepEqual(selected,['cursor_inspired']);
props={...props,busy:true};render();assert.equal(button('Use Cursor inspired').props.disabled,true);
props={...props,selection:{profile_id:'cursor_inspired'},busy:false};render();render();
assert.ok(text().includes('Selected for the next turn'));assert.equal(button('Use Cursor inspired'),undefined);
""")


def test_prompt_api_selection_is_scoped_and_late_save_cannot_change_another_chat():
    run_js(r"""
const calls=[];let finishSave;
const api=async(url,options={})=>{
 const body=options.body?JSON.parse(options.body):null;calls.push({url,body});
 if(url==='/api/models')return catalog;
 if(url.endsWith('/model'))return {model:catalog.models[0],selection:catalog.default_selection};
 if(url==='/api/prompts')return promptCatalog;
 if(url.endsWith('/prompt')&&!body)return {profile_id:'descriptive'};
 if(url==='/api/requests/r-one/prompt'&&body)return new Promise(resolve=>finishSave=resolve);
 if(url==='/api/prompts/preferences')return {...promptCatalog,default_profile_id:body.default_profile_id};
 throw Error(`Unexpected API ${url}`);
};
Component=ModelControl;props={api,requestId:'r-one',onNotice:notice=>assert.fail(notice.text)};
render();await settle();
nodes().find(node=>node.props.className==='mc-trigger').props.onClick();render();
assert.equal(text(button('Agent promptDescriptive')),'Agent promptDescriptive');
button('Agent promptDescriptive').props.onClick();render();
let manager=nodes().find(node=>node.type===ManagerDialog);
const saving=manager.props.onPromptSelect('minimal');
assert.deepEqual(calls.at(-1),{url:'/api/requests/r-one/prompt',body:{profile_id:'minimal'}});
props={...props,requestId:'r-two'};render();await settle();
finishSave({profile_id:'minimal'});await saving;render();
nodes().find(node=>node.props.className==='mc-trigger').props.onClick();render();
assert.ok(button('Agent promptDescriptive'));assert.equal(button('Agent promptMinimal'),undefined);
// A new conversation changes its default, without touching an existing conversation.
props={...props,requestId:null};render();await settle();
nodes().find(node=>node.props.className==='mc-trigger').props.onClick();render();
button('Agent promptDescriptive').props.onClick();render();
manager=nodes().find(node=>node.type===ManagerDialog);
await manager.props.onPromptSelect('minimal');render();
assert.deepEqual(calls.at(-1),{url:'/api/prompts/preferences',body:{default_profile_id:'minimal'}});
assert.equal(nodes().find(node=>node.type===ManagerDialog).props.promptSelection.profile_id,'minimal');
""")


def test_prompt_settings_tabs_support_arrow_navigation_and_failure_is_recoverable():
    run_js(r"""
Component=ManagerDialog;props={catalog,busy:false,initialTab:'models',promptCatalog,promptSelection:{profile_id:'descriptive'},onClose(){}};
render();
const tablist=()=>nodes().find(node=>node.props.role==='tablist');
tablist().props.onKeyDown({key:'End',preventDefault(){}});render();
assert.equal(nodes().find(node=>node.props.role==='tab'&&node.children.includes('Agent prompt')).props['aria-selected'],true);
tablist().props.onKeyDown({key:'ArrowRight',preventDefault(){}});render();
assert.equal(nodes().find(node=>node.props.role==='tab'&&node.children.includes('Models')).props['aria-selected'],true);
// Prompt failure has its own retry; models remain usable.
hooks=[];effects=[];Component=ModelControl;let promptFails=true;
const api=async url=>{
 if(url==='/api/models')return catalog;
 if(url==='/api/prompts'){if(promptFails)throw Error('offline');return promptCatalog}
 throw Error(url);
};
props={api,requestId:null};render();await settle();
nodes().find(node=>node.props.className==='mc-trigger').props.onClick();render();
assert.ok(button('Agent promptTry again'));button('Agent promptTry again').props.onClick();render();
let manager=nodes().find(node=>node.type===ManagerDialog);assert.equal(manager.props.promptError,true);
promptFails=false;await manager.props.onPromptRetry();render();
manager=nodes().find(node=>node.type===ManagerDialog);assert.equal(manager.props.promptError,false);
assert.equal(manager.props.promptSelection.profile_id,'descriptive');
""")


def test_context_usage_separates_output_limit_from_window_and_explains_auto_compaction():
    run_js(r"""
window.React.Component=class{};
globalThis.React=window.React;
globalThis.ReactDOM={createRoot:()=>({render(){}})};
const {ContextUsage}=await import('../../src/vwb/web/static/app.js');
Component=ContextUsage;
props={usage:{context:{limit_tokens:1000000,used_tokens:42000,max_output_tokens:128000,
 auto_compaction_enabled:true,auto_compact_tokens:850000,compaction_count:2},compact_available:false}};
render();nodes().find(node=>node.props.className==='context-trigger').props.onClick();render();
assert.ok(text().includes('Output limit per call128,000'));
assert.ok(text().includes('separate from the context window'));
assert.ok(text().includes('Automatic compaction runs at 850,000 input tokens'));
assert.ok(text().includes('Automatic compactions2'));
assert.equal(nodes().find(node=>node.props.className==='compact-button').props.disabled,true);
props={usage:{context:{},compact_available:false}};render();
assert.ok(!text().includes('Output limit per call'));
assert.ok(!text().includes('Automatic compactions'));
assert.ok(text().includes('Manual compaction is available for Codex conversations'));
""")
