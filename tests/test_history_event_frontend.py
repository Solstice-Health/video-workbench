"""History reset epochs and delayed browser responses, without sockets or providers."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vwb.agents.events import emit
from vwb.web.server import Handler, Workbench

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def run_js(source):
    if NODE is None:
        pytest.skip("Node is optional for frontend checks")
    setup = r"""
import assert from 'node:assert/strict';
let hooks=[],cursor=0,effects=[],root,view,calls=[],timerId=0;
const timers=new Map(), waits=[];
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
globalThis.setTimeout=(callback,ms)=>{const id=++timerId;timers.set(id,{callback,ms});return id;};
globalThis.clearTimeout=id=>timers.delete(id);
globalThis.requestAnimationFrame=callback=>callback();
const event=(seq,text='activity',id=`item-${seq}`)=>({seq,kind:'thinking',actor:'job_lead',
 ts:'2026-10-28T12:00:01Z',data:{id,text}});
function makeSnapshot(epoch=0,seq=70,id='r-test') {
 return {history_epoch:epoch,request:{id,history_revision:epoch+2,created:'2026-10-28T12:00:00Z',chat:[
  {id:'human',role:'human',text:'Brief',ts:'2026-10-28T12:00:00Z'},
  {id:'reply',role:'job_lead',text:epoch?'Retained reply':'Future reply',ts:'2026-10-28T12:00:02Z'}]},
  events:[event(seq,epoch?'New activity':'Removed activity')],cursor:seq,events_before:seq,events_has_more:true,
  asks:[],queue:[],active:null,outputs:[]};
}
let current=makeSnapshot();
function hold(match) {
 let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});
 const wait={match,promise,resolve,reject,started:false};waits.push(wait);return wait;
}
globalThis.fetch=async(url,options={})=>{
 calls.push({url,options});
 const held=waits.find(wait=>!wait.started&&wait.match(url,options));
 let result;
 if(held){held.started=true;result=await held.promise;}
 else if(url==='/api/bootstrap')result={csrf_token:'test'};
 else if(url==='/api/requests')result={requests:[{id:'r-test',title:'Test'},{id:'r-other',title:'Other'}]};
 else if(url.includes('/imports'))result={jobs:[]};
 else if(url.includes('/features'))result={skills:[],folders:[],files:[]};
 else if(url.endsWith('/history'))result={request_id:'r-test'};
 else {
  const parsed=new URL(url,'http://local'),query=parsed.searchParams;
  result=structuredClone(current);
  result.request.id=parsed.pathname.split('/')[3];
  const reset=query.has('history_epoch')&&Number(query.get('history_epoch'))!==current.history_epoch;
  if(!reset&&query.get('events')==='0') {
   for(const key of ['events','cursor','events_before','events_has_more'])delete result[key];
  } else if(!reset&&parsed.pathname.endsWith('/events')) {
   delete result.request;delete result.outputs;
   if(query.has('after')) {
    result.events=result.events.filter(row=>row.seq>Number(query.get('after')));
    result.cursor=Math.max(result.cursor,Number(query.get('after')));
    delete result.events_before;delete result.events_has_more;result.events_after_has_more=false;
   } else result.events=result.events.filter(row=>row.seq<Number(query.get('before')));
  }
 }
 return{ok:true,json:async()=>structuredClone(result)};
};
const {mergeEventPage}=await import('../../src/vwb/web/static/app.js');
function all(node){return node&&typeof node==='object'?[node,...(node.children||[]).flatMap(all)]:[];}
function words(node){return node==null||typeof node==='boolean'?'':typeof node==='object'?(node.children||[]).map(words).join(''):String(node);}
const App=all(root).find(node=>node.type?.name==='App').type;
function render(){cursor=0;view=App();const pending=effects;effects=[];pending.forEach(run=>run());return view;}
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const component=name=>all(view).find(node=>node.type?.name===name);
const data=()=>component('WorkspaceCanvas').props.renderOutputs().props.data;
const older=()=>all(view).find(node=>node.type==='button'&&words(node)==='Load earlier activity').props.onClick();
function poll(){const timer=[...timers].find(([id,row])=>row.callback.name==='poll');
 assert.ok(timer,'A poll must be scheduled');timers.delete(timer[0]);return timer[1].callback();}
async function mount(){render();await flush();render();await flush();render();}
async function readInFlight(kind) {
 if(kind==='metadata')for(let i=0;i<7;i++){await poll();await flush();render();}
 const wait=hold(url=>kind==='metadata'?url.includes('?events=0'):kind==='older'?url.includes('before='):url.includes('/events?after='));
 const task=kind==='older'?older():poll();await flush();assert.ok(wait.started);return{wait,task};
}
async function rewind() {
 const actions=component('MessageActions');actions.props.onAction('rewind',actions.props.message);render();
 current=makeSnapshot(5,1);
 await component('HistoryDialog').props.onSubmit({action:'rewind',message_id:'human'});
 await flush();render();
}
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", setup + source],
                            cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("older", [False, True])
def test_merge_resets_epoch_including_empty_feed_and_rejects_obsolete_pages(older):
    run_js(f"const olderPage={str(older).lower()};" + r"""
const before=makeSnapshot(), fresh=makeSnapshot(5,1);
const reset=mergeEventPage(before,fresh,{older:olderPage});
assert.equal(reset.cursor,1);assert.equal(reset.history_epoch,5);
assert.deepEqual(reset.events,fresh.events);assert.equal(reset.events_before,1);
assert.deepEqual(reset.request,fresh.request);
assert.equal(mergeEventPage(reset,before,{older:olderPage}),reset);
const empty=mergeEventPage(before,{...fresh,events:[],cursor:0,events_before:null,events_has_more:false},{older:olderPage});
assert.deepEqual(empty.events,[]);assert.equal(empty.cursor,0);assert.equal(empty.events_before,null);
assert.equal(empty.events_has_more,false);
const metadata=mergeEventPage(reset,{history_epoch:5,request:{...fresh.request,history_revision:8}});
assert.equal(metadata.cursor,1);assert.equal(metadata.events,reset.events);
const prior=mergeEventPage(reset,{history_epoch:5,events:[event(0)],cursor:999,events_before:0,events_has_more:false},{older:true});
assert.equal(prior.cursor,1);assert.deepEqual(prior.events.map(row=>row.seq),[0,1]);
""")


@pytest.mark.parametrize("kind", ["poll", "metadata", "older"])
@pytest.mark.parametrize("failed", [False, True])
def test_local_rewind_fences_delayed_reads_and_failures(kind, failed):
    run_js(f"const kind='{kind}',failed={str(failed).lower()};" + r"""
await mount();const old=structuredClone(current),pending=await readInFlight(kind);
await rewind();assert.equal(data().cursor,1);assert.equal(data().history_epoch,5);
if(failed)pending.wait.reject(new Error('obsolete failure'));else pending.wait.resolve(old);
await pending.task;await flush();render();
assert.equal(data().history_epoch,5);assert.equal(data().cursor,1);
assert.deepEqual(data().events,current.events);assert.deepEqual(data().request,current.request);
assert.ok(!words(view).includes('obsolete failure'));
assert.ok(!words(view).includes('Loading earlier activity'));
current={...current,events:[...current.events,event(2,'Live after rewind')],cursor:2};
await poll();await flush();render();
assert.equal(data().cursor,2);assert.ok(data().events.some(row=>row.seq===2));
const request=calls.filter(call=>call.url.includes('/events?after=')).at(-1).url;
assert.ok(request.includes('after=1&history_epoch=5'));
""")


@pytest.mark.parametrize("kind", ["poll", "metadata", "older"])
def test_obsolete_response_cannot_repopulate_cleared_snapshot_before_reload(kind):
    run_js(f"const kind='{kind}';" + r"""
await mount();const old=structuredClone(current),pending=await readInFlight(kind);
const fresh=hold(url=>url==='/api/requests/r-test');
const change=rewind();await flush();assert.ok(fresh.started);render();
assert.equal(data(),null);
pending.wait.resolve(old);await pending.task;await flush();render();
assert.equal(data(),null); // No epoch is loaded yet: the request generation must reject it.
fresh.resolve(current);await change;
assert.equal(data().history_epoch,5);assert.deepEqual(data().events,current.events);
""")


@pytest.mark.parametrize("discovery", ["poll", "older"])
def test_other_tab_reset_replaces_snapshot_and_fences_concurrent_reads(discovery):
    run_js(f"const discovery='{discovery}';" + r"""
await mount();const old=structuredClone(current);
const pending=await readInFlight(discovery==='poll'?'older':'poll');
current=makeSnapshot(6,3);
if(discovery==='poll')await poll();else await older();
await flush();render();
assert.equal(data().history_epoch,6);assert.equal(data().cursor,3);
assert.deepEqual(data().request,current.request);assert.deepEqual(data().events,current.events);
pending.wait.resolve(old);await pending.task;await flush();render();
assert.equal(data().cursor,3);assert.deepEqual(data().events,current.events);
current={...current,cursor:4,events:[...current.events,event(4,'Next event')]};
await poll();await flush();render();
assert.equal(data().cursor,4);assert.ok(data().events.some(row=>row.seq===4));
""")


def test_metadata_refresh_discovers_reset_without_a_local_history_action():
    run_js(r"""
await mount();const pending=await readInFlight('metadata');
assert.ok(calls.some(call=>call.url.endsWith('?events=0&history_epoch=0')));
current=makeSnapshot(7,2);pending.wait.resolve(current);await pending.task;await flush();render();
assert.equal(data().history_epoch,7);assert.equal(data().cursor,2);
assert.deepEqual(data().request,current.request);assert.deepEqual(data().events,current.events);
await poll();await flush();
assert.ok(calls.some(call=>call.url.includes('/events?after=2&history_epoch=7')));
""")


@pytest.mark.parametrize("kind", ["metadata", "older"])
def test_leaving_and_reopening_same_conversation_fences_prior_visit(kind):
    run_js(f"const kind='{kind}';" + r"""
await mount();const pending=await readInFlight(kind),old=structuredClone(current);
component('ProjectNavigation').props.onChoose('r-other');await flush();render();
// No rewind: returning to the same conversation must still fence the old visit.
current=makeSnapshot(0,71);current.request.chat[1].text='Updated while away';
component('ProjectNavigation').props.onChoose('r-test');await flush();render();
pending.wait.resolve(old);await pending.task;await flush();render();
assert.equal(data().request.id,'r-test');assert.equal(data().history_epoch,0);
assert.equal(data().cursor,71);assert.deepEqual(data().events,current.events);
assert.deepEqual(data().request,current.request);
""")


@pytest.fixture
def history_app(tmp_path):
    app = Workbench(tmp_path)
    try:
        ws = app.workspace(app.draft_workspace()["request_id"])
        ws.add_chat("human", "Retained")
        ws.add_chat("job_lead", "Removed")
        for index in range(70):
            emit(ws, "thinking", {"id": f"old-{index}", "text": "Removed activity"})
        yield app, ws
    finally:
        app.local_imports.close()
        app.studio.close()
        app.queue.close()


def get(app, path):
    """Exercise real GET routing in memory, without binding a loopback socket."""
    handler = Handler.__new__(Handler)
    handler.server = SimpleNamespace(workbench=app)
    handler.path, handler.command = path, "GET"
    handler._check_origin = lambda *args: None
    handler._json = lambda value: value
    return handler._route()


@pytest.mark.parametrize("query", ["/events?after=70", "/events?before=60&limit=2", "?events=0"])
@pytest.mark.parametrize("new_events", [0, 3])
def test_old_epoch_cursors_get_current_snapshot_through_actual_get_route(history_app, query, new_events):
    app, ws = history_app
    before = get(app, f"/api/requests/{ws.id}")
    assert before["history_epoch"] == 0 and before["cursor"] == 70
    req = ws.load()
    app.change_history(ws, {"action": "rewind", "message_id": req.chat[0].id,
                           "expected_revision": req.history_revision, "operation_id": "reset"})
    for index in range(new_events):
        emit(ws, "thinking", {"id": f"new-{index}", "text": "New activity"})
    current = get(app, f"/api/requests/{ws.id}{query}&history_epoch=0")
    assert current["history_epoch"] == req.history_revision + 1
    assert current["cursor"] == new_events
    assert all(row["data"]["text"] == "New activity" for row in current["events"])
    assert len(current["events"]) == min(new_events, 2 if "limit=2" in query else 300)
    assert [turn["text"] for turn in current["request"]["chat"]] == ["Retained"]
    epoch = current["history_epoch"]
    emit(ws, "thinking", {"id": "continuation", "text": "Next"})
    continued = get(app, f"/api/requests/{ws.id}/events?after={new_events}&history_epoch={epoch}")
    assert continued["history_epoch"] == epoch
    assert [row["seq"] for row in continued["events"]] == [new_events + 1]
    assert "request" not in continued


def test_same_epoch_metadata_stays_lightweight_and_invalid_epoch_is_rejected(history_app, monkeypatch):
    app, ws = history_app
    monkeypatch.setattr("vwb.web.server.event_page", lambda *args, **kwargs: pytest.fail("Unexpected event read"))
    result = get(app, f"/api/requests/{ws.id}?events=0&history_epoch=0")
    assert result["history_epoch"] == 0
    assert "events" not in result and "cursor" not in result
    for epoch in ("-1", "not-an-integer"):
        with pytest.raises(ValueError):
            get(app, f"/api/requests/{ws.id}/events?after=70&history_epoch={epoch}")
