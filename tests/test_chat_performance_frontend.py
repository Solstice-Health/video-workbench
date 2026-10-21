"""Long chats replace streaming snapshots and defer expensive history details."""
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
let hooks=[],cursor=0;
globalThis.React={Component:class{},createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
 useState(initial){const at=cursor++;if(!(at in hooks))hooks[at]=initial;return[hooks[at],value=>hooks[at]=value]},
 useEffect(){},useRef:()=>({current:null})};
globalThis.window={React,Eeva:{Button:'Button',Icon:'Icon',SolsticeMark:'SolsticeMark',AgentMessage:'AgentMessage'}};
globalThis.ReactDOM={createRoot:()=>({render(){}})};
globalThis.document={getElementById:()=>({})};
const {coalesceEvents,mergeEventPage,activityIndex,ToolCall,Activity}=await import('../../src/vwb/web/static/app.js');
function all(node){return node&&typeof node==='object'?[node,...(node.children||[]).flatMap(all)]:[]}
const event=(seq,kind='thinking',id='live',actor='main',extra={})=>({seq,kind,actor,ts:`2026-10-21T12:00:${String(seq%60).padStart(2,'0')}Z`,data:{id,...extra}});
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_streaming_snapshots_are_replaced_in_memory_with_kind_and_actor_identity():
    run_js(r"""
const raw=Array.from({length:12000},(_,i)=>event(i+1,'thinking','long-thought','main',{text:'x'.repeat(1000+i%200)}));
const latest=coalesceEvents(raw);
assert.equal(latest.length,1);assert.equal(latest[0],raw.at(-1));
assert.ok(JSON.stringify(latest).length<2000);assert.ok(JSON.stringify(raw).length>12000000);
const others=[event(12001,'tool','long-thought','main',{name:'same-id-tool'}),event(12002,'thinking','long-thought','child',{text:'child'})];
const merged=coalesceEvents(latest,others,raw.slice(0,2));
assert.equal(merged.length,3);assert.equal(merged[0],raw.at(-1));
assert.deepEqual(merged.map(row=>row.seq),[12000,12001,12002]);
// Completed command records retain their own arguments/output, including across repeated pages.
const complete=event(12003,'tool','long-thought','main',{name:'same-id-tool',status:'completed',arguments:{command:'true'},output:'done'});
const final=coalesceEvents(merged,[complete]);
assert.equal(final.length,3);assert.equal(final.at(-1).data.output,'done');
""")


def test_older_activity_preserves_live_cursor_and_incremental_pages_preserve_history_state():
    run_js(r"""
const before={request:{id:'r-one'},events:[event(500)],cursor:510,events_before:500,events_has_more:true,active:{id:'running'}};
const polled=mergeEventPage(before,{events:[event(511)],cursor:511,events_after_has_more:true});
assert.equal(polled.events.length,1);assert.equal(polled.events[0].seq,511);
assert.equal(polled.events_before,500);assert.equal(polled.events_has_more,true);assert.equal(polled.cursor,511);
const older=mergeEventPage(polled,{events:[event(100,'tool','older')],cursor:999,events_before:100,events_has_more:false},{older:true});
assert.equal(older.cursor,511);assert.equal(older.active.id,'running');assert.equal(older.events_before,100);
assert.equal(older.events_has_more,false);assert.deepEqual(older.events.map(row=>row.seq),[100,511]);
const metadata=mergeEventPage(older,{request:{id:'r-one',message:'updated'},outputs:[]});
assert.equal(metadata.events,older.events);assert.equal(metadata.cursor,511);assert.equal(metadata.events_before,100);
const refreshed=mergeEventPage(older,{events:[event(600,'tool','new')],cursor:600,events_before:400,events_has_more:true},{snapshot:true});
assert.equal(refreshed.events_before,100);assert.equal(refreshed.events_has_more,false);assert.equal(refreshed.cursor,600);
""")


def test_activity_ranges_use_saved_timestamps_and_closed_tool_details_do_not_mount_payloads():
    run_js(r"""
const rows=[event(1,'thinking','one','main',{text:'first'}),event(2,'tool','two','main',{name:'bash'}),event(3,'message','three','main',{text:'reply'})];
const select=activityIndex([...rows,{seq:4,kind:'usage',ts:rows[2].ts,data:{}}]);
assert.deepEqual(select(rows[0].ts,rows[1].ts),rows.slice(0,2));
assert.equal(select(rows[0].ts,rows[1].ts),select(rows[0].ts,rows[1].ts));
const tool=event(4,'tool','payload','main',{name:'bash',status:'completed',arguments:{command:'x'.repeat(5000)},output:'large result'.repeat(10000)});
let view=ToolCall({event:tool});
assert.equal(all(view).filter(node=>node.type==='pre').length,0);
assert.equal(all(view).find(node=>node.props.className==='tool-preview').children[0].length,160);
const opened={open:true};view.props.onToggle({target:opened,currentTarget:opened});cursor=0;view=ToolCall({event:tool});
assert.equal(all(view).filter(node=>node.type==='pre').length,2);
assert.equal(all(view).filter(node=>node.type==='pre')[1].children[0],tool.data.output);
const closed={open:false};view.props.onToggle({target:closed,currentTarget:closed});cursor=0;view=ToolCall({event:tool});
assert.equal(all(view).filter(node=>node.type==='pre').length,0);
// Thinking remains available in full when deliberately expanded.
hooks=[];cursor=0;const thought=event(5,'thinking','full','main',{text:'reasoning '.repeat(20000)});
view=Activity({events:[thought],working:true});
assert.equal(all(view).filter(node=>node.props.className==='thinking-stream').length,0);
all(view).find(node=>node.props.className==='thinking-toggle').props.onClick();cursor=0;view=Activity({events:[thought],working:true});
const stream=all(view).find(node=>node.props.className==='thinking-stream');
assert.equal(all(stream).find(node=>node.type==='p'&&node.children[0]===thought.data.text).children[0].length,thought.data.text.length);
""")
