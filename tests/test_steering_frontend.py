"""Exercise question controls and the actual App submission callbacks without models."""

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
let hooks=[],cursor=0,effects=[],root,view,calls=[];
function memo(callback,deps){const at=cursor++,prior=hooks[at];
 if(!prior||deps.some((value,i)=>value!==prior.deps[i]))hooks[at]={deps,value:callback()};
 return hooks[at].value;}
globalThis.React={Component:class{},createElement:(type,props,...children)=>({type,props:{...props,children:children.flat(Infinity)},children:children.flat(Infinity)}),
 useState(initial){const at=cursor++;if(!(at in hooks))hooks[at]=typeof initial==='function'?initial():initial;
  return[hooks[at],value=>hooks[at]=typeof value==='function'?value(hooks[at]):value];},
 useRef(initial){const at=cursor++;return hooks[at]??={current:initial};},
 useMemo:memo,useCallback:(callback,deps)=>memo(()=>callback,deps),
 useEffect(callback,deps){const at=cursor++,prior=hooks[at];
  if(!prior||!deps||deps.some((value,i)=>value!==prior.deps[i]))effects.push(()=>{prior?.cleanup?.();hooks[at]={deps,cleanup:callback()};});}
};
globalThis.window={React,Eeva:new Proxy({},{get:(_,name)=>name}),innerWidth:1280,addEventListener(){},removeEventListener(){}};
globalThis.ReactDOM={createRoot:()=>({render(value){root=value;}})};
globalThis.document={getElementById:()=>null,querySelector:()=>null,addEventListener(){},removeEventListener(){},hidden:false};
globalThis.localStorage=globalThis.sessionStorage={getItem:()=>null,setItem(){}};
globalThis.location={hash:'#r-test',pathname:'/'};
globalThis.history={replaceState(){}};
globalThis.setTimeout=()=>1;globalThis.clearTimeout=()=>{};
globalThis.requestAnimationFrame=callback=>callback();
const ticket={id:'ask-test',status:'open',mode:'non_blocking',kind:'allow',question:'Allow this change?',
 options:[{id:'grant',label:'Allow'},{id:'deny',label:'Deny'}],waiters:{}};
let snapshot={request:{id:'r-test',chat:[]},asks:[ticket],active:{id:'active',kind:'message',can_stop:true},events:[],queue:[]};
globalThis.fetch=async(url,options={})=>{
 calls.push({url,options,body:options.body&&typeof options.body==='string'?JSON.parse(options.body):options.body});
 let result=url==='/api/bootstrap'?{csrf_token:'test'}:url==='/api/requests'?{requests:[{id:'r-test',title:'Test'}]}:
  url==='/api/uploads'?{upload:{id:'upload-test',name:'notes.txt',kind:'text'}}:
  url.includes('/imports')?{jobs:[]}:url.endsWith('/answers')?{delivery:'waiting_tool'}:
  url.endsWith('/messages')?{job:{id:'queued'}}:snapshot;
 return{ok:true,json:async()=>result};
};
const {QuestionQueue,SteeringAction,questionWaiting}=await import('../../src/vwb/web/static/app.js');
function all(node){return node&&typeof node==='object'?[node,...(node.children||[]).flatMap(all)]:[];}
function words(node){return node==null||typeof node==='boolean'?'':typeof node==='object'?(node.children||[]).map(words).join(''):String(node);}
const App=all(root).find(node=>node.type?.name==='App').type;
function render(){cursor=0;view=App();const pending=effects;effects=[];pending.forEach(run=>run());return view;}
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const composer=()=>all(view).find(node=>node.type==='Composer');
function questionButton(label){return all(QuestionQueue(composer().props.question.props)).find(node=>node.type==='button'&&words(node)===label);}
function purpose(label){questionButton(label).props.onClick();render();}
const steering=()=>all(view).find(node=>node.type===SteeringAction);
const submissions=()=>calls.filter(call=>call.options.method==='POST'&&/\/(answers|messages)$/.test(call.url));
async function mount(){render();await flush();render();await flush();render();}
async function attach(){const file={name:'notes.txt',size:5};all(view).find(node=>node.type==='input'&&node.props.type==='file').props.onChange({target:{files:[file],value:''}});await flush();render();}
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", setup + source],
                            cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("mode", ["now", "queue"])
def test_answers_and_instructions_have_separate_drafts_payloads_and_attachments(blocking, mode):
    run_js(f"const blocking={str(blocking).lower()},mode='{mode}';" + r"""
if(blocking){ticket.mode='blocking';ticket.waiters={live:{expires_at:Date.now()/1000+60}};}
await mount();
assert.equal(questionWaiting(ticket),blocking);
purpose('New instruction');
assert.equal(composer().props.label,'Message the agent');
assert.equal(composer().props.question.props.hidden,false); // The ask card stays open.
composer().props.onValueChange('A separate instruction');render();
await attach();
assert.ok(words(composer().props.attachments).includes('notes.txt'));
purpose('Answer question');
assert.equal(composer().props.value,'');
composer().props.question.props.setAnswer('deny');render();
composer().props.onValueChange('Only change the intro');render();
assert.equal(composer().props.canSend,true);
let action=SteeringAction(steering().props);
assert.equal(action.props.disabled,false);
assert.ok(words(action).includes('Answer now'));
assert.ok(action.props.title.includes(blocking?'Resume the waiting agent':'Stop the current turn'));
if(mode==='now')await action.props.onClick();else await composer().props.onSend(composer().props.value);
await flush();render();
let sent=submissions().at(-1);
assert.ok(sent.url.endsWith('/answers'));
assert.deepEqual(sent.body,{ticket:ticket.id,option:'deny',text:'Only change the intro',mode});
assert.ok(words(composer().props.attachments).includes('notes.txt')); // Answer did not consume uploads.
purpose('New instruction');
assert.equal(composer().props.value,'A separate instruction');
action=SteeringAction(steering().props);
assert.ok(words(action).includes('Send now'));assert.equal(action.props.disabled,false);
if(mode==='now')await action.props.onClick();else await composer().props.onSend(composer().props.value);
await flush();render();
sent=submissions().at(-1);
assert.ok(sent.url.endsWith('/messages'));
assert.equal(sent.body.message,'A separate instruction');
assert.equal(sent.body.mode,mode);assert.deepEqual(sent.body.attachments,['upload-test']);
assert.ok(!('ticket' in sent.body));assert.ok(!('option' in sent.body));
assert.equal(composer().props.value,'');assert.ok(!words(composer().props.attachments).includes('notes.txt'));
""")


def test_general_steer_never_consumes_a_drafted_permission_answer():
    run_js(r"""
ticket.mode='blocking';ticket.waiters={live:{expires_at:Date.now()/1000+60}};
await mount();
composer().props.question.props.setAnswer('grant');render();
composer().props.onValueChange('Only this action');render();
purpose('New instruction');
composer().props.onValueChange('Keep working on the other scene');render();
await SteeringAction(steering().props).props.onClick();await flush();render();
assert.ok(submissions().at(-1).url.endsWith('/messages'));
purpose('Answer question');
assert.equal(composer().props.question.props.answer,'grant');
assert.equal(composer().props.value,'Only this action');
assert.equal(submissions().length,1);
""")


def test_waiting_answers_can_send_now_for_terminal_turn_but_instructions_cannot_stop_it():
    run_js(r"""
snapshot.active={kind:'external',can_stop:false};
ticket.mode='blocking';ticket.waiters={live:{expires_at:Date.now()/1000+60}};
await mount();
let action=SteeringAction(steering().props);
assert.ok(action);assert.equal(action.props.disabled,true);
composer().props.question.props.setAnswer('deny');render();
action=SteeringAction(steering().props);
assert.equal(action.props.disabled,false);
await action.props.onClick();await flush();render();
assert.ok(submissions().at(-1).url.endsWith('/answers'));
purpose('New instruction');
assert.equal(SteeringAction(steering().props),null);
assert.equal(questionWaiting({...ticket,waiters:{expired:{expires_at:Date.now()/1000-1}}}),false);
assert.equal(questionWaiting({...ticket,status:'answered'}),false);
""")
