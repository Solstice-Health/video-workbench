"""Fresh-context recheck UI contracts; no provider or paid jobs are used.

Browser integration fixture: run this file with the project's Python to print a
GET /api/requests/qc-recheck-fixture/qc response. Serve the three targets' URLs
from local test videos (v1/v2: 120 frames at 30 fps; v3: 240 frames at 60 fps), or
replace only the URLs. Open QC > Review video & findings > Compare & recheck.
The latest version intentionally needs explicit boundaries. v2 prefills [12,36)
but still requires confirmation. The audit finding cannot be handed to Edit.
Mock POST /qc/rechecks with {recheck, job} and count writes: opening, scrubbing,
switching targets and polling must make none. Poll in an updated pin or revision
to fence submission; poll in completed evidence to render exact sampled frames.
"""

import json

from test_qc_frontend import run_js


def browser_snapshot():
    original = dict(
        target="output:main@v1", output="main", version=1, fps=30,
        frames=120, width=1920, height=1080, sha256="a" * 64,
        canvas_file_sha256="b" * 64, url="/qc-fixture-v1.mp4",
    )
    newer = dict(
        original, target="output:main@v2", version=2,
        sha256="c" * 64, canvas_file_sha256="d" * 64, url="/qc-fixture-v2.mp4",
    )
    latest = dict(
        original, target="output:main@v3", version=3, fps=60, frames=240,
        sha256="e" * 64, canvas_file_sha256="f" * 64, url="/qc-fixture-v3.mp4",
    )
    finding = dict(
        id="finding-title", revision=2, status="needs_recheck", lane="shared",
        category="visual", priority="warning", expected="The whole title is visible.",
        observed="The title is clipped on the right.",
        anchor=dict(original, f0=12, f1=36, region=[0.1, 0.2, 0.7, 0.25]),
    )
    record = dict(
        id="recheck-title", item_id=finding["id"], item_revision=2,
        item=finding, before=finding["anchor"],
        after=dict(newer, f0=20, f1=44, region=finding["anchor"]["region"]),
        status="completed", model="fixture-review-model", provider="fixture-provider",
        created_at="2026-10-28T10:00:00Z", completed_at="2026-10-28T10:00:10Z",
        stale=True, stale_reason="Version 3 was rendered after this review.",
        job=dict(id="qc-job-title", status="completed"),
        result=dict(
            assessment="not_observed", summary="The sampled title frames show the full text.",
            qualification="unqualified_visual_review", coverage="sampled_frames_only",
            evidence=[
                dict(target=original["target"], sha256=original["sha256"],
                     canvas_file_sha256=original["canvas_file_sha256"],
                     frames=[12, 24, 35], coverage="sampled frames only"),
                dict(target=newer["target"], sha256=newer["sha256"],
                     canvas_file_sha256=newer["canvas_file_sha256"],
                     frames=[20, 32, 43], coverage="sampled frames only"),
            ],
        ),
    )
    return dict(
        request=dict(id="qc:qc-recheck-fixture", chat=[]),
        targets=[latest, newer, original], items=[finding, dict(finding, id="audit-title", lane="audit")],
        rechecks=[record], active=None, queue=[], events=[], usage={},
        model=dict(model="fixture-review-model", provider="fixture-provider"),
    )


def check_js(source):
    run_js("const fixture=" + json.dumps(browser_snapshot()) + r""";
const {newerTargets,comparisonDraft,comparisonBody,comparisonFrames,findingMarkers,recheckStaleReason,RecheckResults,ComparisonPlayer}=qc;
const original=fixture.targets[2], newer=fixture.targets[1], latest=fixture.targets[0], finding=fixture.items[0];
const view=session=>QCSurface({session,requestId:'qc-recheck-fixture',visible:true,Activity:'Activity'});
const sessionFor=(api,extra={})=>createQCSession('qc-recheck-fixture',api,{...timers,...extra});
""" + source)


def test_versions_and_windows_are_explicit_and_never_scaled_across_clocks():
    check_js(r"""
const other={...latest,target:'output:other@v9',output:'other',version:9};
const all=[original,newer,latest,other,{...latest,version:0},{...latest,version:1}];
assert.deepEqual(newerTargets(finding,all).map(t=>t.target),[latest.target,newer.target]);
assert.equal(all[0],original); // Sorting does not mutate the snapshot.
const selected=comparisonDraft(finding,fixture.targets);
assert.equal(selected.after.target,latest.target);assert.equal(selected.f0,'');assert.equal(selected.f1,'');
assert.equal(selected.confirmed,false);assert.notEqual(selected.item,finding);
selected.item.anchor.region[0]=0;assert.equal(finding.anchor.region[0],.1);
for(const changed of [{fps:29.97},{frames:121},{fps:60,frames:240}]){
 const draft=comparisonDraft(finding,[],{...newer,...changed});
 assert.equal(draft.f0,'');assert.equal(draft.f1,'');
}
const same=comparisonDraft(finding,fixture.targets,newer);
assert.equal(same.f0,'12');assert.equal(same.f1,'36');assert.equal(same.confirmed,false);
const noTarget=comparisonDraft(finding,[original]);assert.equal(noTarget.after,null);
""")


def test_recheck_body_fences_both_pins_revision_output_and_window_bounds():
    check_js(r"""
const draft={...comparisonDraft(finding,fixture.targets,latest),f0:'50',f1:'80'};
const body=comparisonBody(draft,fixture.items,fixture.targets);
assert.deepEqual(body,{item_id:finding.id,expected_revision:2,target:latest.target,
 pin:{sha256:latest.sha256,canvas_file_sha256:latest.canvas_file_sha256},f0:50,f1:80});
assert.throws(()=>comparisonBody(draft,[{...finding,revision:3}],fixture.targets),/finding changed/);
assert.throws(()=>comparisonBody(draft,[],fixture.targets),/finding changed/);
for(const key of ['sha256','canvas_file_sha256']){
 for(const pin of [original,latest]){
  const targets=fixture.targets.map(t=>t===pin?{...t,[key]:'replacement'}:t);
  assert.throws(()=>comparisonBody(draft,fixture.items,targets),/pin changed/);
 }
}
assert.throws(()=>comparisonBody(draft,fixture.items,[latest,newer]),/original pin/);
for(const after of [original,{...latest,output:'other',target:'output:other@v3'}]){
 assert.throws(()=>comparisonBody({...draft,after},fixture.items,[original,after]),/newer rendered version/);
}
for(const patch of [{f0:''},{f0:null},{f0:' '},{f0:1.5},{f0:-1},{f0:240},{f1:241},{f1:50},{f1:''}]){
 assert.throws(()=>comparisonBody({...draft,...patch},fixture.items,fixture.targets));
}
assert.throws(()=>comparisonBody({...draft,after:{...latest,fps:Infinity}},fixture.items,fixture.targets),/frame rate/);
assert.throws(()=>comparisonBody({...draft,item:{...finding,anchor:{...finding.anchor,region:[0,0,2,1]}}},fixture.items,fixture.targets),/region/);
""")


def test_paired_frame_scrubbing_uses_seconds_from_starts_and_holds_shorter_window():
    check_js(r"""
const before={...original,f0:12,f1:36}, after={...latest,f0:100,f1:220};
assert.deepEqual(comparisonFrames(before,after,0),{before:12,after:100,offset:0,max:119/60,step:1/60});
const half=comparisonFrames(before,after,.5);assert.equal(half.before,27);assert.equal(half.after,130);
const one=comparisonFrames(before,after,1);assert.equal(one.before,35);assert.equal(one.after,160);
const end=comparisonFrames(before,after,999);assert.equal(end.before,35);assert.equal(end.after,219);
assert.equal(comparisonFrames(before,after,-1).before,12);
assert.equal(comparisonFrames(before,after,NaN).after,100);
for(const fps of [24,29.97,30,59.94,60,120]){
 const window={...latest,fps,f0:60,f1:190};
 for(const frame of [60,61,77,189])assert.equal(comparisonFrames(before,window,(frame-60)/fps).after,frame);
 for(let i=0;i<250;i++){
  const p=comparisonFrames(before,window,i/61);
  assert.ok(p.before>=12&&p.before<36);assert.ok(p.after>=60&&p.after<190);
 }
}
const single=comparisonFrames({...before,f1:13},{...after,f1:101},100);
assert.equal(single.max,0);assert.equal(single.before,12);assert.equal(single.after,100);
assert.throws(()=>comparisonFrames({...before,f1:12},after));
""")


def test_timeline_markers_require_exact_pins_and_seek_original_frame_and_region():
    check_js(r"""
const mismatches=[{...finding,id:'new',anchor:{...finding.anchor,...newer}},
 {...finding,id:'different-bytes',anchor:{...finding.anchor,sha256:'other'}},
 {...finding,id:'different-canvas',anchor:{...finding.anchor,canvas_file_sha256:'other'}},
 {...finding,id:'invalid-frame',anchor:{...finding.anchor,f1:121}}];
assert.deepEqual(findingMarkers([finding,...mismatches],original).map(m=>[m.item.id,m.left,m.width]),[[finding.id,10,20]]);
const session=sessionFor(async()=>({...fixture,items:[finding,...mismatches]}));const stop=session.start(()=>{});await flush();
session.draft({...selectTarget(original),f0:'0',region:null});
const nodes=walk(view(session)),marker=nodes.find(n=>n.props['aria-label']?.startsWith(`Review finding ${finding.id} at`));
assert.ok(marker);assert.equal(nodes.filter(n=>n.props.className==='qc-timeline-marker').length,1);
marker.props.onClick();assert.equal(session.state.draft.f0,'12');assert.equal(session.state.draft.f1,'36');
assert.deepEqual(session.state.draft.region,finding.anchor.region);assert.equal(session.state.draft.seek,1);
stop();
""")


def test_open_compare_confirm_and_poll_never_submit_paid_work_or_handoff_audit():
    check_js(r"""
const calls=[];const session=sessionFor(async(url,options={})=>{calls.push({url,options});return fixture;});
const stop=session.start(()=>{});await flush();
session.openComparison('audit-title');session.comparisonTarget(newer.target);session.confirmComparison(true);
await session.refresh();await session.refresh();
assert.ok(calls.every(c=>!c.options.method));assert.deepEqual(session.state.selected,[]);
assert.equal(session.state.comparison.item.lane,'audit');
const nodes=walk(view(session));assert.ok(words(nodes).includes('Audit · private'));
assert.equal(nodes.find(n=>n.props['aria-label']==='Select finding audit-title for editor').props.disabled,true);
const revision=session.state.data.items[1].revision;
session.closeComparison();session.openComparison('audit-title');
assert.equal(session.state.comparison.item.revision,revision);assert.equal(session.state.comparison.after.target,newer.target);
assert.ok(calls.every(c=>!c.options.method));stop();
""")


def test_explicit_confirmation_and_changes_fence_recheck_submission():
    check_js(r"""
const writes=[];const session=sessionFor(async(url,options={})=>{
 if(!options.method)return fixture;writes.push({url,body:JSON.parse(options.body)});
 return{recheck:{id:'accepted'},job:{id:'job'}};
});const stop=session.start(()=>{});await flush();
session.openComparison(finding.id);await session.recheck();assert.equal(writes.length,0);
session.comparisonWindow({f0:'90',f1:'150'});await session.recheck();assert.equal(writes.length,0);
assert.match(session.state.error,/Confirm/);
session.confirmComparison(true);session.comparisonWindow({f0:'91'});
assert.equal(session.state.comparison.confirmed,false);await session.recheck();assert.equal(writes.length,0);
session.confirmComparison(true);await session.recheck();await flush();
assert.equal(writes[0].url,'/api/requests/qc-recheck-fixture/qc/rechecks');
assert.equal(writes[0].body.f0,91);assert.equal(writes[0].body.f1,150);
assert.equal(session.state.comparison.confirmed,false);assert.equal(session.state.data.items[0].status,'needs_recheck');
await session.recheck();assert.equal(writes.length,1);
session.confirmComparison(true);session.comparisonTarget(newer.target);
assert.equal(session.state.comparison.confirmed,false);assert.equal(session.state.comparison.f0,'12');stop();
""")


def test_uncertain_recheck_retry_keeps_key_but_payload_edits_rotate_it():
    check_js(r"""
const writes=[];let serial=0,fail=true;
const session=sessionFor(async(url,options={})=>{
 if(!options.method)return fixture;writes.push(JSON.parse(options.body));
 if(fail)throw Error('Response lost after acceptance');return{recheck:{id:'existing'},job:{id:'job'}};
},{uuid:()=>`recheck-${++serial}`});const stop=session.start(()=>{});await flush();
session.openComparison(finding.id);session.comparisonTarget(newer.target);session.confirmComparison(true);
await session.recheck();await flush();assert.equal(session.state.comparison.confirmed,true);
session.closeComparison();session.openComparison(finding.id);await session.refresh();
await session.recheck();await flush();assert.deepEqual(writes[0],writes[1]);
session.comparisonWindow({f0:'13'});session.confirmComparison(true);await session.recheck();await flush();
assert.notEqual(writes[1].idempotency_key,writes[2].idempotency_key);
session.comparisonWindow({f0:'12'});session.confirmComparison(true);await session.recheck();await flush();
assert.notEqual(writes[0].idempotency_key,writes[3].idempotency_key); // Returning to old input is a new intent.
fail=false;await session.recheck();await flush();assert.deepEqual(writes[3],writes[4]);
session.confirmComparison(true);await session.recheck();await flush();assert.notEqual(writes[4].idempotency_key,writes[5].idempotency_key);
stop();
""")


def test_inflight_recheck_is_guarded_and_late_completion_stays_with_own_session():
    check_js(r"""
const pending=deferred(),writes=[];
const a=sessionFor(async(url,options={})=>{if(!options.method)return fixture;writes.push(options);return pending.promise;});
const stop=a.start(()=>{});await flush();a.openComparison(finding.id);a.comparisonTarget(newer.target);a.confirmComparison(true);
const sent=a.recheck();await a.recheck();assert.equal(writes.length,1);stop();
const b=createQCSession('another',async()=>snap('another'),timers);const stopB=b.start(()=>{});await flush();b.draft({text:'Other chat'});
pending.resolve({recheck:{id:'late'},job:{id:'job'}});await sent;await flush();
assert.equal(a.state.comparison.confirmed,false);assert.equal(b.state.comparison,null);assert.equal(b.state.draft.text,'Other chat');
assert.equal(b.state.info,'');stopB();
""")


def test_numeric_formatting_preserves_retry_key_and_closed_findings_only_compare():
    check_js(r"""
let snapshot=structuredClone(fixture),serial=0;const writes=[];
const session=sessionFor(async(url,options={})=>{
 if(!options.method)return snapshot;writes.push(JSON.parse(options.body));throw Error('Lost response');
},{uuid:()=>`key-${++serial}`});const stop=session.start(()=>{});await flush();
session.openComparison(finding.id);session.comparisonTarget(newer.target);session.confirmComparison(true);
await session.recheck();await flush();
session.comparisonWindow({f0:'012',f1:'036'});session.confirmComparison(true);await session.recheck();await flush();
assert.deepEqual(writes[0],writes[1]);
snapshot.items[0].status='resolved';snapshot.items[0].revision++;
await session.refresh();session.reloadComparison();session.confirmComparison(true);await session.recheck();
assert.equal(writes.length,2);assert.match(session.state.error,/Reopen/);
const nodes=walk(view(session));assert.equal(nodes.filter(n=>n.props['aria-label']?.endsWith('comparison preview')).length,2);
assert.equal(nodes.find(n=>n.type==='Button'&&words(n)==='Recheck with fresh context').props.disabled,true);
stop();
""")


def test_poll_pin_changes_hide_comparison_media_and_fence_confirmed_submission():
    check_js(r"""
for(const changed of ['before','after','revision']){
 let snapshot=structuredClone(fixture);const writes=[];
 const session=sessionFor(async(url,options={})=>{if(!options.method)return snapshot;writes.push(options);return{};});
 const stop=session.start(()=>{});await flush();session.openComparison(finding.id);session.comparisonTarget(newer.target);session.confirmComparison(true);
 if(changed==='revision')snapshot.items[0].revision++;
 else snapshot.targets.find(t=>t.target===(changed==='before'?original.target:newer.target)).sha256='new-bytes';
 await session.refresh();await session.recheck();assert.equal(writes.length,0);
 const nodes=walk(view(session));
 assert.ok(!nodes.some(n=>n.props['aria-label']?.endsWith('comparison preview')));
 assert.equal(nodes.find(n=>n.type==='Button'&&words(n)==='Recheck with fresh context').props.disabled,true);
 session.reloadComparison();assert.equal(session.state.comparison.confirmed,false);
 if(changed==='before')assert.throws(()=>comparisonBody(session.state.comparison,snapshot.items,snapshot.targets),/original pin/);
 else assert.doesNotThrow(()=>comparisonBody(session.state.comparison,snapshot.items,snapshot.targets));
 stop();
}
""")


def test_result_labels_attribution_actual_coverage_and_staleness_never_resolve():
    check_js(r"""
for(const [assessment,label] of [['persists','Issue observed'],['not_observed','Not observed in sampled frames'],['inconclusive','Inconclusive']]){
 const record=structuredClone(fixture.rechecks[0]);record.result.assessment=assessment;
 const tree=RecheckResults({item:finding,rechecks:[record],targets:fixture.targets}),text=words(tree).replace(/\s+/g,' '),nodes=walk(tree);
 for(const expected of [label,'Unqualified visual review','Outdated recheck','Version 3 was rendered','fixture-review-model','fixture-provider','Finding revision 2','12, 24, 35','20, 32, 43','Sampled frames only','does not resolve'])assert.ok(text.includes(expected),expected);
 assert.ok(!nodes.some(n=>n.type==='Button'||n.props.onClick)); // Results cannot mutate disposition.
 assert.ok(!text.includes('passed'));assert.equal(finding.status,'needs_recheck');
}
const record={...fixture.rechecks[0],stale:false};
assert.match(recheckStaleReason(record,finding,fixture.targets),/newer rendered version/);
assert.equal(recheckStaleReason(record,finding,[original,newer]),'');
assert.match(recheckStaleReason(record,{...finding,revision:3},[original,newer]),/finding changed/);
assert.match(recheckStaleReason(record,finding,[original,{...newer,sha256:'oops'}]),/Pinned media/);
const missing={...record,result:{...record.result,evidence:[]}};
assert.match(words(RecheckResults({item:finding,rechecks:[missing],targets:[original,newer]})),/No sampled frame evidence reported/);
const foreign={...record,result:{...record.result,evidence:[{...record.result.evidence[0],sha256:'wrong'}]}};
assert.match(words(RecheckResults({item:finding,rechecks:[foreign],targets:[original,newer]})),/Unmatched pin/);
""")


def test_prepared_failed_and_interrupted_jobs_use_existing_qc_controls():
    check_js(r"""
for(const status of ['queued','running','failed','stopped']){
 const record={...fixture.rechecks[0],status:'prepared',result:null,stale:false,job:{id:'job',status,error:status==='failed'?'Provider rejected request':''}};
 const text=words(RecheckResults({item:finding,rechecks:[record],targets:fixture.targets})).replace(/\s+/g,' ');
 assert.ok(text.includes('Recheck pending'));assert.ok(!text.includes('Not observed in sampled frames'));
 assert.ok(text.includes(`job · ${status}`));
 if(status==='failed')assert.ok(text.includes('Provider rejected request'));
}
const session=sessionFor(async()=>({...fixture,active:{id:'job',can_stop:true},rechecks:[{...fixture.rechecks[0],status:'prepared',result:null}]}));
const stop=session.start(()=>{});await flush();const composer=walk(view(session)).find(n=>n.type==='Composer');
assert.equal(composer.props.working,true);assert.equal(typeof composer.props.onStop,'function');stop();
""")


def test_comparison_ui_requires_confirmation_and_shows_range_and_region_caveats():
    check_js(r"""
const session=sessionFor(async()=>fixture);const stop=session.start(()=>{});await flush();session.openComparison(finding.id);
let tree=view(session),nodes=walk(tree),text=words(nodes);
assert.ok(text.includes('Choose both newer frame boundaries explicitly'));
assert.equal(nodes.find(n=>n.type==='Button'&&words(n)==='Recheck with fresh context').props.disabled,true);
assert.ok(!nodes.some(n=>n.props['aria-label']?.endsWith('comparison preview')));
session.comparisonWindow({f0:'90',f1:'150'});tree=view(session);nodes=walk(tree);text=words(nodes);
assert.ok(text.includes('does not track matching content'));assert.ok(text.includes('original normalized region'));
assert.ok(text.includes('Opening this comparison does not run a model'));
const videos=nodes.filter(n=>n.props['aria-label']?.endsWith('comparison preview'));
assert.equal(videos.length,2);assert.ok(videos.every(n=>!n.props.controls&&n.props.muted));
let pauses=0;for(const [i,node] of videos.entries()){
 const element={readyState:1,currentTime:0,pause(){pauses++;}};
 node.props.onLoadedMetadata({currentTarget:element});
 assert.equal(element.currentTime,frameTime(i===0?12:90,i===0?original:latest));
 node.props.onPlay({currentTarget:element});
}
assert.equal(pauses,4);
assert.equal(nodes.filter(n=>n.type==='svg'&&n.props.className==='qc-region-overlay').length,2);
const confirm=nodes.find(n=>n.props.className==='qc-confirm').props.children.find(n=>n?.type==='input');
confirm.props.onChange({target:{checked:true}});nodes=walk(view(session));
assert.equal(nodes.find(n=>n.type==='Button'&&words(n)==='Recheck with fresh context').props.disabled,false);
stop();
""")


def test_comparison_frame_fields_keep_intermediate_text_and_commit_on_blur_or_enter():
    check_js(r"""
// Drive the real component handlers through stateful renders/effects, rather
// than the static hook stubs used for tree inspection in the other tests.
let rendering;
window.React={...window.React,
 useState(initial){
  const owner=rendering,index=owner.cursor++;
  if(!(index in owner.slots))owner.slots[index]=typeof initial==='function'?initial():initial;
  return[owner.slots[index],value=>{
   const next=typeof value==='function'?value(owner.slots[index]):value;
   if(!Object.is(next,owner.slots[index])){owner.slots[index]=next;owner.dirty=true;}
  }];
 },
 useRef(initial){const owner=rendering,index=owner.cursor++;return owner.slots[index]??=( {current:initial} );},
 useEffect(effect,deps){
  const owner=rendering,index=owner.cursor++,previous=owner.slots[index];
  if(!previous||deps.some((value,i)=>!Object.is(value,previous[i]))){owner.slots[index]=deps;owner.effects.push(effect);}
 }
};
const fresh=await import('../../src/vwb/web/static/qc-panel.js?frame-input-regression');
function mount(Component,props){
 const owner={slots:[],cursor:0,effects:[],dirty:false};
 return {props,render(){
  let tree,passes=0;
  do{
   assert.ok(++passes<10,'render/effect loop');owner.dirty=false;owner.cursor=0;rendering=owner;
   tree=Component(this.props);for(const effect of owner.effects.splice(0))effect();
  }while(owner.dirty);
  return tree;
 }};
}
function descendants(tree){return[tree,...(tree?.props?.children||[]).flatMap(child=>typeof child==='object'&&child?descendants(child):[])];}
const before={...original,f0:12,f1:36},after={...latest,f0:100,f1:220};
const player=mount(fresh.ComparisonPlayer,{before,after,onError:message=>assert.fail(message)});
const lanes=descendants(player.render()).filter(node=>node.type?.name==='ComparisonFrame');
assert.equal(lanes.length,2);
for(const [index,sequence,expected,commitMode] of [[0,['','2','20'],20,'blur'],[1,['','1','15','150'],150,'enter']]){
 const calls=[],lane=lanes[index],window=index===0?before:after;
 const component=mount(lane.type,{...lane.props,onFrame:offset=>calls.push(offset)});
 const field=()=>descendants(component.render()).find(node=>node.type==='input');
 field().props.onFocus?.();
 for(const value of sequence){
  field().props.onChange({target:{value}});
  assert.equal(String(field().props.value),value,`${lane.props.label} must retain intermediate ${JSON.stringify(value)}`);
  assert.equal(calls.length,0,'typing does not seek');
 }
 const element={value:sequence.at(-1)};
 let prevented=false;
 if(commitMode==='enter')field().props.onKeyDown({key:'Enter',currentTarget:element,preventDefault(){prevented=true;}});
 else field().props.onBlur({currentTarget:element});
 assert.equal(calls.length,1);assert.equal(calls[0],(expected-window.f0)/window.fps);
 if(commitMode==='enter')assert.equal(prevented,true);
 // Parent applies the offset. Later blur must not submit the same seek twice.
 component.props={...component.props,frame:expected};
 assert.equal(String(field().props.value),String(expected));
 field().props.onBlur({currentTarget:element});assert.equal(calls.length,1);
 // Invalid/empty completed edits restore the selected frame and never seek.
 for(const invalid of ['',String(window.f0-1),String(window.f1),'20.5']){
  field().props.onFocus();field().props.onChange({target:{value:invalid}});
  assert.equal(String(field().props.value),invalid);
  field().props.onBlur({currentTarget:{value:invalid}});
  assert.equal(String(field().props.value),String(expected));assert.equal(calls.length,1);
 }
 // Scrubbing the other lane updates an idle field, but a poll/rerender must
 // not replace an in-progress edit. Invalid blur restores the latest frame.
 component.props={...component.props,frame:window.f0+1};
 assert.equal(String(field().props.value),String(window.f0+1));
 field().props.onFocus();field().props.onChange({target:{value:'2'}});
 component.props={...component.props,frame:window.f0+2};
 assert.equal(String(field().props.value),'2');
 field().props.onBlur({currentTarget:{value:'2'}});
 assert.equal(String(field().props.value),String(window.f0+2));assert.equal(calls.length,1);
}
""")


if __name__ == "__main__":
    print(json.dumps(browser_snapshot(), indent=2))
