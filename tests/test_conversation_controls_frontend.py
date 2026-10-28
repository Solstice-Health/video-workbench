"""Standalone project/history UI contracts, interaction guards and payload boundaries."""

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
let frame, id = 0;
const element = (type, props, ...children) => ({type, props:{...props, children:children.flat(Infinity)}});
globalThis.document = {body:{}, activeElement:null};
globalThis.window = {React:{
  createElement:element,
  useState(initial) {
    const owner=frame, slot=owner.cursor++;
    if(!(slot in owner.slots)) owner.slots[slot]=typeof initial==='function'?initial():initial;
    return [owner.slots[slot], value=>{owner.slots[slot]=typeof value==='function'?value(owner.slots[slot]):value;}];
  },
  useRef(initial) {const slot=frame.cursor++; return frame.slots[slot] ||= {current:initial};},
  useId() {const slot=frame.cursor++; return frame.slots[slot] ||= `cc-test-${++id}`;},
  useEffect(effect,deps) {
    const slot=frame.cursor++, old=frame.slots[slot];
    if(!old || deps.some((value,index)=>value!==old.deps[index])) {
      old?.cleanup?.(); frame.slots[slot]={deps,effect};
    }
  },
}, Eeva:{Button:'Button'}, ReactDOM:{createPortal:node=>node}};
const ui=await import('../../src/vwb/web/static/conversation-controls.js');
const {groupConversations,pendingTurns,messageActions,attachmentCount,historyPayload,projectPayload,
  ProjectNavigation,ProjectDialog,MessageActions,HistoryDialog}=ui;
const human={id:'human-1',role:'human',text:'Original message',attachments:[{id:'file-1',name:'Reference.png'}]};
const assistant={id:'assistant-2',role:'job_lead',text:'A saved reply'};
const projects=[{id:'p1',name:'Campaign'},{id:'p2',name:'Launch'},{id:'p3',name:'Empty project'}];
const requests=[{id:'r1',title:'First draft',project_id:'p1'}, {id:'r2',title:'Second draft',project_id:'p1'},
  {id:'r3',title:'Launch teaser',project_id:'p2'}, {id:'r4',title:'Ungrouped',project_id:null}];
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return{promise,resolve,reject}};
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve()};
function words(node) {
  if(node==null||typeof node==='boolean')return '';
  if(Array.isArray(node))return node.map(words).join(' ');
  return typeof node==='object'?words(node.props.children):String(node);
}
function walk(node,nodes=[]) {
  if(Array.isArray(node)){node.forEach(item=>walk(item,nodes));return nodes;}
  if(!node||typeof node!=='object')return nodes;
  nodes.push(node);walk(node.props.children,nodes);return nodes;
}
// Small stateful React host: component/key-specific hooks let tests exercise actual callbacks and rerenders.
function mount(Component,props={},effects=false) {
  const frames=new Map(), hosts=new Map(); let currentProps=props, tree;
  function expand(node,path,seen) {
    if(Array.isArray(node))return node.map((item,i)=>expand(item,`${path}/${i}`,seen));
    if(!node||typeof node!=='object')return node;
    const key=`${path}/${typeof node.type==='function'?node.type.name:node.type}:${node.props.key||''}`;
    if(typeof node.type==='function') {
      const owner=frames.get(key)||{slots:[]}; frames.set(key,owner);seen.add(key);
      frame=owner;owner.cursor=0;
      return expand(node.type(node.props),key,seen);
    }
    const result={...node,props:{...node.props,children:expand(node.props.children,key,seen)}};
    if(result.props.ref) {
      let host=hosts.get(key);
      if(!host) {
        host={open:false,isConnected:true,showModal(){this.open=true;},close(){this.open=false;},
          focus(){document.activeElement=this;},
          querySelector(){return {focus(){host.focusedSafeAction=true;}}}};
        hosts.set(key,host);
      }
      result.props.ref.current=host;
    }
    return result;
  }
  function render(patch={}) {
    currentProps={...currentProps,...patch}; const seen=new Set();
    tree=expand(element(Component,currentProps),'root',seen);
    for(const [key,owner] of frames)if(!seen.has(key)) {
      owner.slots.forEach(slot=>slot?.cleanup?.());frames.delete(key);
    }
    if(effects)for(const owner of frames.values())for(const slot of owner.slots)if(slot?.effect) {
      slot.cleanup=slot.effect();delete slot.effect;
    }
    return tree;
  }
  render();
  return {render,get tree(){return tree;},get nodes(){return walk(tree);},
    button(label){const found=walk(tree).find(node=>node.type==='Button'&&words(node).trim()===label);assert.ok(found,`Missing button: ${label}`);return found;},
    unmount(){for(const owner of frames.values())owner.slots.forEach(slot=>slot?.cleanup?.());frames.clear();}};
}
const submitForm=view=>view.nodes.find(node=>node.type==='form').props.onSubmit({preventDefault(){}});
const setField=(view,type,value)=>view.nodes.find(node=>node.type===type).props.onChange({target:{value}});
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_grouping_search_empty_projects_and_missing_project_never_drop_requests():
    run_js(r"""
const orphan={id:'orphan',title:'Missing project',project_id:'deleted'};
const all=[...requests,orphan], before=JSON.stringify([projects,all]);
const groups=groupConversations(projects,all);
assert.deepEqual(groups.projects.map(group=>group.requests.map(row=>row.id)),[['r1','r2'],['r3'],[]]);
assert.deepEqual(groups.conversations.map(row=>row.id),['r4','orphan']);
assert.equal(groups.projects[0].requests[0],requests[0]);
assert.deepEqual(groupConversations(projects,all,'  cAMPAign ').projects[0].requests.map(row=>row.id),['r1','r2']);
assert.deepEqual(groupConversations(projects,all,'second').projects.map(group=>[group.project.id,group.requests.map(row=>row.id)]),[['p1',['r2']]]);
assert.deepEqual(groupConversations(projects,all,'r4').conversations.map(row=>row.id),['r4']);
assert.equal(groupConversations(projects,all,'no-match').projects.length,0);
assert.equal(groupConversations(projects,all,'Empty').projects[0].requests.length,0);
assert.equal(JSON.stringify([projects,all]),before);
""")


def test_message_roles_stable_ids_pending_and_attachment_preserving_payloads():
    run_js(r"""
for(const role of ['human','user'])assert.deepEqual(messageActions({...human,role}),['fork','rewind','edit']);
for(const role of ['assistant','job_lead'])assert.deepEqual(messageActions({...assistant,role}),['fork','rewind']);
for(const message of [{text:'Synthetic answer',role:'human'},{...human,role:'tool'},null])assert.deepEqual(messageActions(message),[]);
for(const operation of ['fork','rewind'])for(const message of [human,assistant]) {
  assert.deepEqual(historyPayload(operation,message),{action:operation,message_id:message.id});
}
assert.deepEqual(historyPayload('edit',human,'  revised\ntext  ',true),{action:'edit',message_id:'human-1',text:'  revised\ntext  ',fork:true});
assert.deepEqual(historyPayload('edit',human,'',false),{action:'edit',message_id:'human-1',text:'',fork:false});
assert.equal(attachmentCount(human),1);assert.equal(attachmentCount(assistant),0);
assert.deepEqual(historyPayload('fork',{...assistant,id:undefined,message_id:'alternate'}),{action:'fork',message_id:'alternate'});
assert.throws(()=>historyPayload('edit',{...human,attachments:[]},' '),/Enter a message/);
assert.throws(()=>historyPayload('edit',assistant,'Rewritten'),/saved message/);
assert.throws(()=>historyPayload('remove',human),/saved message/);
for(const pending of [{active:{id:'job'}},{pending:true},{pending:2},{queue:[{id:'q'}]},{queued:1},{queue_count:1},{status:'queued'},{status:'working'}]) {
  assert.equal(pendingTurns(pending),true);assert.throws(()=>historyPayload('fork',{...human,...pending}),/active and pending/);
}
assert.equal(pendingTurns({pending:0,queue:[],active:null,status:'complete'}),false);
assert.deepEqual(human.attachments,[{id:'file-1',name:'Reference.png'}]);
""")


def test_message_action_handlers_include_any_saved_message_and_guard_disabled():
    run_js(r"""
const calls=[];
for(const message of [human,assistant]) {
  const view=mount(MessageActions,{message,onAction:(...args)=>calls.push(args)});
  assert.equal(view.nodes[0].props.role,'group');
  for(const action of messageActions(message))view.button({fork:'Fork from here',rewind:'Rewind to here',edit:'Edit message'}[action]).props.onClick();
}
assert.deepEqual(calls.map(([action,message])=>[action,message.id]),[['fork','human-1'],['rewind','human-1'],['edit','human-1'],['fork','assistant-2'],['rewind','assistant-2']]);
for(const patch of [{disabled:true},{message:{...human,pending:true}}]) {
  const view=mount(MessageActions,{message:human,onAction:()=>assert.fail('Disabled action ran'),...patch});
  for(const node of view.nodes.filter(n=>n.type==='Button')){assert.equal(node.props.disabled,true);node.props.onClick();}
}
assert.equal(mount(MessageActions,{message:{role:'human',text:'No stable ID'}}).tree,null);
""")


def test_history_edit_choices_submit_exact_payload_and_no_native_confirmation():
    run_js(r"""
const sent=[],view=mount(HistoryDialog,{operation:'edit',message:human,onSubmit:payload=>sent.push(payload)});
const text=words(view.tree);
assert.match(text,/removes this message and everything after it/);
assert.match(text,/history before this message/);assert.match(text,/Current workspace files stay as-is/);
assert.match(text,/do not roll back the filesystem/);assert.match(text,/1\s+attachment\s+on this message/);
assert.equal(view.nodes.find(n=>n.type==='textarea').props.value,human.text);
setField(view,'textarea','New\nmessage');view.render();
await view.button('Edit + continue').props.onClick();view.render();
await view.button('Edit + new conversation').props.onClick();
assert.deepEqual(sent,[{action:'edit',message_id:'human-1',text:'New\nmessage',fork:false},{action:'edit',message_id:'human-1',text:'New\nmessage',fork:true}]);
assert.ok(!view.nodes.some(n=>n.type==='form')); // Enter in the textarea is a newline, not a destructive submit.
assert.equal(view.button('Cancel').props['data-cc-initial-focus'],'true');
assert.equal(view.button('Edit + continue').props.variant,'destructive');
for(const operation of ['fork','rewind']) {
  const dialog=mount(HistoryDialog,{operation,message:assistant,onSubmit:payload=>sent.push(payload)});
  assert.match(words(dialog.tree),/through and including this message/);
  await dialog.button(operation==='fork'?'Fork conversation':'Rewind conversation').props.onClick();
  assert.deepEqual(sent.at(-1),{action:operation,message_id:assistant.id});
}
""")


def test_history_duplicate_submit_busy_cancel_errors_retry_and_target_reset():
    run_js(r"""
const gate=deferred(),sent=[];let closed=0;
const view=mount(HistoryDialog,{operation:'edit',message:human,onSubmit:payload=>{sent.push(payload);return gate.promise;},onClose:()=>closed++});
const button=view.button('Edit + continue'),first=button.props.onClick();
await button.props.onClick();view.button('Cancel').props.onClick();
assert.equal(sent.length,1);assert.equal(closed,0);
view.render();assert.equal(view.nodes.find(n=>n.type==='textarea').props.disabled,true);
for(const node of view.nodes.filter(n=>n.type==='Button'))assert.equal(node.props.disabled,true);
gate.reject(Error('History changed. Reload before retrying.'));await first;view.render();
assert.match(words(view.nodes.find(n=>n.props.role==='alert')),/History changed/);
setField(view,'textarea','My retained draft');view.render({busy:true});
await view.button('Edit + continue').props.onClick();view.button('Cancel').props.onClick();assert.equal(sent.length,1);assert.equal(closed,0);
view.render({busy:false,onSubmit:payload=>sent.push(payload)});
await view.button('Edit + new conversation').props.onClick();view.render();
assert.equal(sent.at(-1).text,'My retained draft');assert.equal(sent.at(-1).fork,true);
view.render({message:{...human,id:'other',text:'Other message'}});
assert.equal(view.nodes.find(n=>n.type==='textarea').props.value,'Other message');
view.button('Cancel').props.onClick();assert.equal(closed,1);
view.render({message:null});assert.equal(view.tree,null);
""")


def test_history_empty_edit_validation_and_parent_error_remain_accessible():
    run_js(r"""
const view=mount(HistoryDialog,{operation:'edit',message:{...human,attachments:[]},error:'Revision is stale',onSubmit:()=>assert.fail('Empty text sent')});
setField(view,'textarea','  ');view.render();
assert.equal(view.button('Edit + continue').props.disabled,true);assert.equal(view.button('Edit + new conversation').props.disabled,true);
await view.button('Edit + continue').props.onClick();view.render();
assert.match(words(view.nodes.find(n=>n.props.role==='alert')),/Enter a message/);
const root=view.nodes.find(n=>n.type==='dialog'),label=view.nodes.find(n=>n.type==='label'),input=view.nodes.find(n=>n.type==='textarea');
assert.equal(root.props['aria-modal'],'true');assert.ok(view.nodes.some(n=>n.props.id===root.props['aria-labelledby']));
assert.ok(view.nodes.some(n=>n.props.id===root.props['aria-describedby']));assert.equal(label.props.htmlFor,input.props.id);
const invalid=mount(HistoryDialog,{operation:'edit',message:assistant,onSubmit:()=>assert.fail('Assistant edited')});
assert.equal(invalid.button('Edit + continue').props.disabled,true);await invalid.button('Edit + continue').props.onClick();
""")


def test_project_validation_disallows_detach_current_missing_or_pending_destinations():
    run_js(r"""
assert.deepEqual(projectPayload('create','  New project  '),{name:'New project'});
assert.deepEqual(projectPayload('move','ignored','p2',projects,requests[0]),{project_id:'p2'});
assert.deepEqual(projectPayload('move',' New project ','new',projects,requests[0]),{name:'New project'});
for(const destination of ['',null,'p1','missing'])assert.throws(()=>projectPayload('move','',destination,projects,requests[0]));
assert.throws(()=>projectPayload('create','  '),/name/);
for(const name of ['a'.repeat(121),'Line\nbreak','Tab\there',123])assert.throws(()=>projectPayload('create',name),/name/);
assert.equal(projectPayload('create','🎬'.repeat(120)).name,'🎬'.repeat(120));
assert.throws(()=>projectPayload('move','', 'p2',projects,null),/conversation/);
assert.throws(()=>projectPayload('move','', 'p2',projects,{...requests[0],queue_count:1}),/active and pending/);
assert.throws(()=>projectPayload('delete','name'),/action/);
""")


def test_navigation_groups_selection_creation_scopes_and_busy_handlers():
    run_js(r"""
const chosen=[],drafts=[];
const view=mount(ProjectNavigation,{projects,requests,selected:'r1',onChoose:id=>chosen.push(id),onCreateConversation:id=>drafts.push(id),onCreateProject:()=>{},onMoveConversation:()=>{}});
assert.ok(view.nodes.some(n=>n.props['aria-label']==='Projects'));
assert.ok(view.nodes.some(n=>n.props['aria-label']==='Conversations without a project'));
const selected=view.nodes.find(n=>n.props['aria-current']==='page');assert.match(words(selected),/First draft/);selected.props.onClick();
assert.deepEqual(chosen,['r1']);
const global=view.nodes.find(n=>n.type==='Button'&&words(n).trim()==='New conversation'&&!n.props['aria-label']);
await global.props.onClick();view.render();
await view.nodes.find(n=>n.props['aria-label']==='New conversation in Empty project').props.onClick();
assert.deepEqual(drafts,[null,'p3']);
view.render({busy:true});
for(const node of view.nodes.filter(n=>n.type==='Button')) {assert.equal(node.props.disabled,true);if(node.props.onClick)await node.props.onClick();}
assert.deepEqual(chosen,['r1']);assert.deepEqual(drafts,[null,'p3']);
view.render({busy:false,requests:requests.map(row=>row.id==='r1'?{...row,queued:1}:row)});
assert.equal(view.button('Move current conversation').props.disabled,true);view.button('Move current conversation').props.onClick();
assert.ok(!view.nodes.some(n=>n.type==='dialog'));
view.render({selected:null,projects:[],requests:[]});assert.match(words(view.tree),/Create a project/);assert.match(words(view.tree),/without a project appear here/);
""")


def test_project_create_move_conflict_retry_reuses_created_destination():
    run_js(r"""
let creates=0,moves=0;const sent=[];
const view=mount(ProjectNavigation,{projects,requests,selected:'r4',onCreateProject:async payload=>{creates++;sent.push(payload);return{project:{id:'created-project'}};},
  onMoveConversation:async(...args)=>{moves++;sent.push(args);if(moves===1)throw Error('Path conflict: rename or choose new project');}});
view.button('Move current conversation').props.onClick();view.render();
assert.match(words(view.tree),/merges this conversation’s files/);assert.match(words(view.tree),/preserving paths/);
assert.match(words(view.tree),/Conversations keep separate histories/);
assert.ok(!view.nodes.some(n=>n.type==='option'&&/no project/i.test(words(n))));
assert.equal(view.button('Move conversation').props.disabled,true);
setField(view,'select','new');view.render();setField(view,'input','New destination');view.render();
submitForm(view);submitForm(view);await flush();view.render();
assert.equal(creates,1);assert.equal(moves,1);assert.match(words(view.tree),/Path conflict/);
assert.ok(view.nodes.some(n=>n.type==='dialog'));submitForm(view);await flush();view.render();
assert.equal(creates,1);assert.equal(moves,2);assert.ok(!view.nodes.some(n=>n.type==='dialog'));
assert.deepEqual(sent,[{name:'New destination'},['r4','created-project'],['r4','created-project']]);
view.button('New project').props.onClick();view.render();setField(view,'input','Another project');view.render();submitForm(view);await flush();view.render();
assert.equal(creates,2);assert.equal(moves,2);assert.ok(!view.nodes.some(n=>n.type==='dialog'));
""")


def test_move_existing_project_explains_workspace_change_and_retains_original_on_switch():
    run_js(r"""
const gate=deferred(),sent=[];
const view=mount(ProjectNavigation,{projects,requests,selected:'r1',onCreateProject:()=>assert.fail('Unneeded project creation'),onMoveConversation:(...args)=>{sent.push(args);return gate.promise;}});
view.button('Move current conversation').props.onClick();view.render();
assert.match(words(view.tree),/destination project’s workspace/);assert.match(words(view.tree),/original files remain in the original project/);
setField(view,'select','p2');view.render();submitForm(view);
view.render({selected:'r3'});assert.ok(!view.nodes.some(n=>n.type==='dialog'));
gate.resolve(true);await flush();view.render();assert.deepEqual(sent,[['r1','p2']]);
const busy=mount(ProjectDialog,{operation:'move',projects,request:{...requests[0],active:true},onSubmit:()=>assert.fail('Moved during active turn')});
setField(busy,'select','p2');busy.render();submitForm(busy);await flush();
assert.equal(busy.button('Move conversation').props.disabled,true);
""")


def test_native_modal_focus_escape_backdrop_busy_and_restore():
    run_js(r"""
let restored=0,closed=0,prevented=0;const prior={isConnected:true,focus(){restored++;}};document.activeElement=prior;
const view=mount(HistoryDialog,{operation:'rewind',message:assistant,onClose:()=>closed++,onSubmit:()=>{}},true);
let dialog=view.nodes.find(n=>n.type==='dialog'),host=dialog.props.ref.current;
assert.equal(host.open,true);assert.equal(host.focusedSafeAction,true);
const first={focus(){document.activeElement=this;}},last={focus(){document.activeElement=this;}};
const keyboardTarget={querySelectorAll:()=>[first,last],focus(){document.activeElement=this;}};
document.activeElement=last;
dialog.props.onKeyDown({key:'Tab',currentTarget:keyboardTarget,preventDefault(){}});assert.equal(document.activeElement,first);
dialog.props.onKeyDown({key:'Tab',shiftKey:true,currentTarget:keyboardTarget,preventDefault(){}});assert.equal(document.activeElement,last);
keyboardTarget.querySelectorAll=()=>[];
dialog.props.onKeyDown({key:'Tab',currentTarget:keyboardTarget,preventDefault(){}});assert.equal(document.activeElement,keyboardTarget);
dialog.props.onCancel({preventDefault(){prevented++;}});assert.equal(closed,1);assert.equal(prevented,1);
view.render({busy:true});dialog=view.nodes.find(n=>n.type==='dialog');
dialog.props.onCancel({preventDefault(){prevented++;}});assert.equal(closed,1);
const target={getBoundingClientRect:()=>({left:10,right:30,top:10,bottom:30})};
dialog.props.onClick({target,currentTarget:target,clientX:0,clientY:0});assert.equal(closed,1);
view.render({busy:false});dialog=view.nodes.find(n=>n.type==='dialog');
dialog.props.onClick({target,currentTarget:target,clientX:20,clientY:20});assert.equal(closed,1); // Padding is not backdrop.
dialog.props.onClick({target,currentTarget:target,clientX:0,clientY:0});assert.equal(closed,2);
view.unmount();assert.equal(host.open,false);assert.equal(restored,1);
""")
