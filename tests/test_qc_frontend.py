"""QC geometry, isolation, request races and explicit human review contracts."""
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
globalThis.window={React:{
 createElement:(type,props,...children)=>({type,props:{...props,children:children.flat(Infinity)}}),
 useState:initial=>[typeof initial==='function'?initial():initial,()=>{}],
 useEffect(){},useRef:initial=>({current:initial})},
 Eeva:{Button:'Button',Composer:'Composer',MessageBar:'MessageBar',UserMessage:'UserMessage',AgentMessage:'AgentMessage'}};
const qc=await import('../../src/vwb/web/static/qc-panel.js');
const {videoRect,videoPoint,dragRegion,validRegion,frameAt,frameTime,frameSelection,findingBody,handoffItems,qcDraft,createQCSession,QCSurface,sameTargetPin,targetChanged,selectTarget}=qc;
const target={target:'output:main@v1',output:'main',version:1,fps:30,width:1920,height:1080,frames:120,sha256:'sha',canvas_file_sha256:'canvas-sha',url:'/video.mp4'};
const snap=(id,extra={})=>({request:{id:id.startsWith('qc:')?id:`qc:${id}`,chat:[]},targets:[target],items:[],queue:[],events:[],...extra});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
const timers={schedule:()=>1,cancel(){},uuid:()=> 'test-key'};
function walk(node,nodes=[]){
 if(node==null||typeof node==='boolean')return nodes;
 if(Array.isArray(node)){node.forEach(n=>walk(n,nodes));return nodes;}
 if(typeof node!=='object')return nodes;
 if(typeof node.type==='function')return walk(node.type(node.props),nodes);
 nodes.push(node);walk(node.props.children,nodes);return nodes;
}
function words(node){
 if(node==null||typeof node==='boolean')return '';
 if(Array.isArray(node))return node.map(words).join(' ');
 if(typeof node!=='object')return String(node);
 return words(node.props.children);
}
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_rectangle_matches_letterboxing_pillarboxing_and_reverse_drag():
    run_js(r"""
const viewport={left:100,top:50,width:400,height:400};
const landscape=videoRect(viewport,1920,1080);
assert.deepEqual(landscape,{left:100,top:137.5,width:400,height:225});
assert.equal(videoPoint([200,100],landscape),null); // Black bar is not video.
assert.equal(dragRegion([200,100],[400,300],landscape),null);
assert.deepEqual(dragRegion([200,193.75],[400,306.25],landscape),[.25,.25,.5,.5]);
assert.deepEqual(dragRegion([400,306.25],[200,193.75],landscape),[.25,.25,.5,.5]);
assert.deepEqual(dragRegion([300,250],[900,900],landscape),[.5,.5,.5,.5]);
assert.equal(dragRegion([300,250],[300,250],landscape),null);
const portrait=videoRect({left:0,top:0,width:400,height:225},1080,1920);
assert.equal(portrait.left,136.71875);
assert.deepEqual(dragRegion([portrait.left,0],[portrait.left+portrait.width,225],portrait),[0,0,1,1]);
assert.equal(videoPoint([100,100],portrait),null);
assert.equal(videoRect(viewport,0,100),null);
assert.equal(videoRect({...viewport,width:0},1920,1080),null);
for(let i=1;i<50;i++){
 const rect=videoRect({left:23,top:41,width:377,height:212},1080,1920);
 const region=dragRegion([rect.left+rect.width*i/50,rect.top+rect.height/4],[999,999],rect);
 assert.ok(validRegion(region)); assert.equal(region[0]+region[2],1);
}
""")


def test_frame_selection_is_half_open_and_rejects_invalid_or_missing_values():
    run_js(r"""
const draft={...qcDraft(),target:target.target,f0:'119'};
assert.deepEqual(frameSelection(draft,target),{target:target.target,f0:119,f1:120});
assert.deepEqual(frameSelection({...draft,selectionMode:'range',f0:'2',f1:'8',region:[.1,.2,.3,.4]},target),{target:target.target,f0:2,f1:8,region:[.1,.2,.3,.4]});
for(const f0 of ['', ' ', null, true, NaN, Infinity, -1, 120, '1.5']) assert.throws(()=>frameSelection({...draft,f0},target));
for(const f1 of ['',null,119,118,121,119.5])assert.throws(()=>frameSelection({...draft,selectionMode:'range',f1},target));
for(const region of [[0,0,0,1],[.9,0,.2,1],[-.1,0,1,1],[0,0,NaN,1],[0,0,'',1],{x:0,y:0,w:1,h:1}])assert.throws(()=>frameSelection({...draft,region},target));
assert.throws(()=>frameSelection(draft,null));
assert.throws(()=>frameSelection(draft,{...target,frames:0}));
assert.equal(frameAt(1,target),30);assert.equal(frameAt(4,target),119);
assert.equal(frameAt(2,{...target,fps:29.97}),59);
assert.equal(frameAt(Infinity,target),null);
assert.equal(frameAt(.133333,target),4);assert.equal(frameAt(.133,target),3);
for(const fps of [24,29.97,30,60,120])for(const frame of [0,1,4,30,119]){
 const media={...target,fps};const time=frameTime(frame,media);
 assert.ok(time>frame/fps&&time<(frame+1)/fps);assert.equal(frameAt(time,media),frame);
}
for(const frame of ['',null,true,-1,120,1.5])assert.equal(frameTime(frame,target),null);
""")


def test_finding_payload_uses_only_selected_immutable_target_and_allowed_fields():
    run_js(r"""
const draft={...qcDraft(),...selectTarget(target),expected:' readable title ',observed:' clipped ',f0:'12',f1:'30',selectionMode:'range',region:[0,.1,1,.8]};
assert.deepEqual(findingBody(draft,[target,{...target,target:'output:main@v2',version:2}]),{
 target:target.target,pin:{sha256:'sha',canvas_file_sha256:'canvas-sha'},f0:12,f1:30,region:[0,.1,1,.8],expected:'readable title',observed:'clipped',category:'visual',priority:'warning',lane:'shared'});
for(const patch of [{target:'output:main@v3'},{expected:' '},{observed:''},{priority:'high'},{category:'Visual issue'},{lane:'private'},{expected:'x'.repeat(4001)}])assert.throws(()=>findingBody({...draft,...patch},[target]));
assert.throws(()=>findingBody(draft,[]));
""")


def test_late_polls_cannot_cross_request_switches_or_replace_new_snapshots():
    run_js(r"""
const calls=[];
const api=(url,options)=>{const d=deferred();calls.push({url,options,...d});return d.promise};
const a=createQCSession('a',api,timers), b=createQCSession('b',api,timers);
a.draft({text:'draft a',region:[0,0,.5,.5]});b.draft({text:'draft b'});
const stopA=a.start(()=>{});stopA();const stopB=b.start(()=>{});
assert.ok(calls[0].options.signal.aborted);
calls[1].resolve(snap('b',{request:{id:'qc:b',chat:[{role:'qc_lead',text:'B'}]}}));await flush();
calls[0].resolve(snap('a',{items:[{id:'late-a'}]}));await flush();
assert.equal(a.state.data,null);assert.equal(b.state.data.request.chat[0].text,'B');
const stopAgain=a.start(()=>{});a.refresh();
calls[3].resolve(snap('qc:a',{events:[{seq:7}],cursor:7}));await flush();
calls[2].resolve(snap('a',{events:[{seq:2}],cursor:2}));await flush();
assert.equal(a.state.data.cursor,7);assert.equal(a.state.draft.text,'draft a');assert.equal(b.state.draft.text,'draft b');
a.refresh();calls[4].resolve(snap('b'));await flush();
assert.equal(a.state.data.cursor,7);assert.match(a.state.pollError,/another conversation/);
stopAgain();stopB();
""")


def test_mutation_invalidates_older_read_and_preserves_text_typed_during_send():
    run_js(r"""
const calls=[];const api=(url,options={})=>{const d=deferred();calls.push({url,options,...d});return d.promise};
const session=createQCSession('one / two',api,timers);const stop=session.start(()=>{});
session.draft({text:'original',uploads:[{id:'upload-1'}]});
const sent=session.send('now');
assert.ok(calls[0].options.signal.aborted);
assert.equal(calls[1].url,'/api/requests/one%20%2F%20two/qc/messages');
assert.deepEqual(JSON.parse(calls[1].options.body),{message:'original',mode:'now',attachments:['upload-1'],idempotency_key:'test-key'});
await session.send();assert.equal(calls.length,2); // Immediate guard against duplicate click.
session.draft({text:'next draft'});
calls[0].resolve(snap('one / two',{active:{id:'old'}}));await flush();assert.equal(session.state.data,null);
calls[1].resolve({job:{id:'sent'}});await sent;await flush();
assert.equal(session.state.draft.text,'next draft');assert.deepEqual(session.state.draft.uploads,[]);
calls[2].resolve(snap('one / two',{active:{id:'new'},events:[{seq:4}]}));await flush();
session.refresh();calls[3].resolve(snap('one / two',{events:[{seq:4}]}));await flush();
assert.equal(session.state.data.events.length,1); // Bounded full pages replace, never append duplicates.
stop();
""")


def test_upload_and_write_completion_stay_with_original_request_after_switch():
    run_js(r"""
const calls=[];const api=(url,options={})=>{const d=deferred();calls.push({url,options,...d});return d.promise};
const a=createQCSession('a',api,timers), b=createQCSession('b',api,timers);
const stopA=a.start(()=>{});calls[0].resolve(snap('a'));await flush();
const upload=a.upload([{name:'review image.png'}]);stopA();
const stopB=b.start(()=>{});b.draft({text:'private b'});
calls[1].resolve({id:'image',upload:{kind:'image'}});await upload;
assert.equal(calls[1].url,'/api/uploads');assert.equal(calls[1].options.raw,true);
assert.equal(a.state.draft.uploads[0].id,'image');assert.deepEqual(b.state.draft.uploads,[]);
a.draft({text:'send A'});const pending=a.send();
calls[3].resolve({job:{id:'a-job'}});await pending;
assert.equal(a.state.draft.text,'');assert.equal(b.state.draft.text,'private b');
assert.equal(calls.filter(call=>call.url.endsWith('/messages')).length,1);
assert.equal(calls[3].url,'/api/requests/a/qc/messages');
stopB();calls[2].resolve(snap('b'));await flush();assert.equal(b.state.data,null);
""")


def test_handoff_filters_audit_closed_and_missing_items_and_retries_same_key():
    run_js(r"""
const items=[{id:'open',revision:1,status:'open',lane:'shared'},
 {id:'recheck',revision:2,status:'needs_recheck',lane:'shared'},
 {id:'audit',revision:1,status:'open',lane:'audit'},
 {id:'resolved',revision:2,status:'resolved',lane:'shared'},
 {id:'dismissed',revision:2,status:'dismissed',lane:'shared'}];
assert.deepEqual(handoffItems(items,['open','open','recheck','audit','resolved','missing']).map(i=>i.id),['open','recheck']);
const writes=[];let fail=true, count=0;
const session=createQCSession('r',async(url,options={})=>{
 if(!options.method)return snap('r',{items});writes.push({url,body:JSON.parse(options.body)});
 if(fail){fail=false;throw Error('Lost response');}return{batch:{id:'batch'}};
},{...timers,uuid:()=>`key-${++count}`});
const stop=session.start(()=>{});await flush();
for(const id of ['open','audit','resolved','recheck','missing'])session.select(id,true);
await session.handoff();await flush();assert.match(session.state.error,/Lost response/);
await session.handoff();await flush();
assert.deepEqual(writes[0],writes[1]);assert.deepEqual(writes[1].body.item_ids,['open','recheck']);
assert.deepEqual(writes[1].body.expected_revisions,{open:1,recheck:2});
assert.equal(count,1);assert.ok(!session.state.selected.includes('open'));assert.ok(session.state.selected.includes('audit'));
await session.handoff();assert.equal(writes.length,2); // Audit-only/closed selection never dispatches.
stop();
""")


def test_stop_resume_remove_and_status_use_only_qc_routes_and_expected_revision():
    run_js(r"""
const calls=[];const session=createQCSession('r',async(url,options={})=>{
 if(!options.method)return snap('r');calls.push({url,method:options.method,body:JSON.parse(options.body)});return{};
},timers);
await session.command('/stop');await session.command('/resume',{job_id:'qc-job'});
await session.command('/resume');await session.command('/queue/queued',{},'DELETE');
session.note('item','I checked the pinned render.');await session.transition({id:'item',revision:9},'resolved');
assert.deepEqual(calls.map(x=>x.url),['/api/requests/r/qc/stop','/api/requests/r/qc/resume','/api/requests/r/qc/resume','/api/requests/r/qc/queue/queued','/api/requests/r/qc/items/item/status']);
assert.deepEqual(calls[1].body,{job_id:'qc-job'});assert.deepEqual(calls[2].body,{});
assert.equal(calls[3].method,'DELETE');
assert.deepEqual(calls[4].body,{status:'resolved',expected_revision:9,note:'I checked the pinned render.'});
assert.match(session.state.info,/human review/);
""")


def test_accessible_review_controls_use_private_attachments_and_human_resolution_language():
    run_js(r"""
const item={id:'audit',revision:1,status:'needs_recheck',lane:'audit',category:'visual',priority:'warning',expected:'readable',observed:'clipped',anchor:{target:target.target,f0:3,f1:4}};
const session=createQCSession('r',async()=>snap('r',{
 model:{model:'configured-qc',provider:'provider'},items:[item],
 request:{id:'qc:r',chat:[{role:'human',text:'Check it',attachments:[{id:'att',name:'Reference',url:'/wrong-edit-url'}]}]},
 active:{id:'qc-active'},queue:[{id:'queued',message:'Next QC message'}]
}),timers);
const stop=session.start(()=>{});await flush();
const view=QCSurface({session,requestId:'r',visible:true,Activity:'Activity'}), nodes=walk(view), text=words(view);
assert.ok(text.includes('newer version requires review'));assert.ok(text.includes('Resolved means human reviewed'));
assert.ok(!text.includes('automated pass'));assert.ok(text.includes('configured-qc'));assert.ok(text.includes('Audit · private'));
const attachment=nodes.find(n=>n.type==='a');assert.equal(attachment.props.href,'/api/requests/r/qc/attachments/att');
const checkbox=nodes.find(n=>n.props.type==='checkbox');assert.equal(checkbox.props.disabled,true);assert.match(checkbox.props['aria-label'],/Select finding audit/);
const composer=nodes.find(n=>n.type==='Composer');assert.equal(composer.props.label,'Message QC');assert.equal(composer.props.working,true);assert.equal(typeof composer.props.onStop,'function');
assert.ok(nodes.some(n=>n.type==='Activity'&&n.props.working));
assert.ok(nodes.some(n=>n.type==='select'&&n.props.value==='warning'));
assert.ok(nodes.some(n=>n.type==='video'&&n.props.src.startsWith(target.url+'?qc_pin=')));
assert.equal(nodes.find(n=>n.type==='Button'&&words(n).includes('Send selected')).props.disabled,true);
stop();
""")


def test_empty_targets_and_unreported_usage_do_not_invent_results_or_model_controls():
    run_js(r"""
const session=createQCSession('r',async()=>snap('r',{targets:[],usage:{}}),timers);
const stop=session.start(()=>{});await flush();
const view=QCSurface({session,requestId:'r',visible:true,Activity:'Activity'}), nodes=walk(view), text=words(view);
assert.match(text,/No rendered targets yet/);assert.match(text,/Not reported/);assert.match(text,/QC model not reported/);
assert.ok(!nodes.some(n=>n.type==='video'));assert.ok(!nodes.some(n=>n.props['aria-label']==='QC model picker'));
assert.equal(nodes.find(n=>n.type==='Composer').props.canSend,false);
stop();
""")


def test_render_pin_changes_require_explicit_reload_and_clear_geometry():
    run_js(r"""
let current=target;const calls=[];
const session=createQCSession('r',async(url,options={})=>{
 if(options.method){calls.push(JSON.parse(options.body));return{item:{id:'new'}};}
 return snap('r',{targets:[current]});
},timers);
const stop=session.start(()=>{});await flush();
session.draft({f0:'10',f1:'20',selectionMode:'range',region:[.1,.2,.3,.4],expected:'whole title',observed:'cut off'});
const pinned=session.state.draft.reviewTarget;
current={...target,sha256:'rerendered'};await session.refresh();
assert.equal(session.state.draft.reviewTarget,pinned);assert.equal(pinned.sha256,'sha');
assert.ok(targetChanged(session.state.draft,session.state.data.targets));
assert.throws(()=>findingBody(session.state.draft,session.state.data.targets),/Reload/);
await session.createFinding();assert.equal(calls.length,0);
const nodes=walk(QCSurface({session,requestId:'r',visible:true,Activity:'Activity'}));
assert.ok(!nodes.some(n=>n.type==='video'));
const reload=nodes.find(n=>n.type==='Button'&&words(n).includes('Reload target'));
assert.ok(reload);reload.props.onClick();
assert.equal(session.state.draft.region,null);assert.equal(session.state.draft.f0,'0');assert.equal(session.state.draft.f1,'1');
assert.equal(session.state.draft.reviewTarget.sha256,'rerendered');
assert.equal(session.state.draft.expected,'whole title');
await session.createFinding();await flush();
assert.deepEqual(calls[0].pin,{sha256:'rerendered',canvas_file_sha256:'canvas-sha'});
current={...current,canvas_file_sha256:'new-composition'};await session.refresh();
assert.ok(targetChanged(session.state.draft,session.state.data.targets));
stop();
""")


def test_new_versions_do_not_retarget_a_review_or_satisfy_an_old_finding_pin():
    run_js(r"""
const newer={...target,target:'output:main@v2',version:2,sha256:'v2'};
let targets=[target];const session=createQCSession('r',async()=>snap('r',{targets}),timers);
const stop=session.start(()=>{});await flush();session.draft({region:[0,0,.5,.5],f0:'9'});
targets=[newer,target];await session.refresh();
assert.equal(session.state.draft.target,target.target);assert.equal(session.state.draft.f0,'9');
assert.deepEqual(session.state.draft.region,[0,0,.5,.5]);assert.equal(targetChanged(session.state.draft,targets),false);
assert.equal(sameTargetPin(target,newer),false);
assert.equal(sameTargetPin(target,{...target,canvas_file_sha256:'changed'}),false);
targets=[newer];await session.refresh();assert.equal(targetChanged(session.state.draft,targets),true);
assert.equal(session.state.draft.target,target.target);
stop();
""")


def test_uncertain_message_send_retries_same_key_and_retains_draft_and_uploads():
    run_js(r"""
let fail=true,serial=0;const calls=[];
const session=createQCSession('r',async(url,options={})=>{
 if(!options.method)return snap('r');calls.push({url,body:JSON.parse(options.body)});
 if(fail){fail=false;throw Error('Response lost after acceptance');}return{job:{id:'accepted'}};
},{...timers,uuid:()=>`message-${++serial}`});
session.draft({text:'  Check these frames  ',uploads:[{id:'one',name:'reference'}]});
await session.send();
assert.equal(session.state.draft.text,'  Check these frames  ');assert.equal(session.state.draft.uploads[0].id,'one');
assert.equal(calls[0].body.message,'Check these frames');
session.draft({text:'Check these frames'});await session.send('now');
assert.equal(calls[0].body.idempotency_key,calls[1].body.idempotency_key);
assert.deepEqual(calls[1].body.attachments,['one']);assert.equal(calls[1].body.mode,'now');
assert.equal(session.state.draft.text,'');assert.deepEqual(session.state.draft.uploads,[]);
// A deliberate identical message after a confirmed success starts a fresh turn.
session.draft({text:'Check these frames',uploads:[{id:'one'}]});await session.send();
assert.notEqual(calls[2].body.idempotency_key,calls[1].body.idempotency_key);
// Drafts changed during a failed send are not rolled back.
const wait=deferred();const late=createQCSession('r2',()=>wait.promise,timers);
late.draft({text:'original'});const pending=late.send();late.draft({text:'new thought'});
wait.reject(Error('offline'));await pending;assert.equal(late.state.draft.text,'new thought');
""")
