"""Editor markers consume frozen handoffs; private QC never crosses the API."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections import OrderedDict
from http.client import HTTPConnection
from pathlib import Path

import pytest
import test_qc_chat_store as fixtures

from vwb.qc import chat_store
from vwb.qc.chat import QCWorkspace
from vwb.qc.chat_store import QCStore
from vwb.util.jsonio import read_json, write_json
from vwb.web.server import Workbench, WorkbenchServer

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
movie = fixtures.movie
ws = fixtures.ws


@pytest.fixture
def app(ws):
    app = Workbench(ws.root.parent)
    app.queue.stop(ws)
    try:
        yield app
    finally:
        app.local_imports.close()
        app.studio.close()
        app.queue.close()


def handoff(store, item, key="sent"):
    return store.handoff([item["id"]], lambda **kwargs: {"id": "fixture-job"}, key)


@pytest.fixture
def read_api(app, ws):
    server = WorkbenchServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def read():
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        # Edit reads require no private QC cookie or capability.
        connection.request("GET", f"/api/requests/{ws.id}")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 200, body
        return json.loads(body)

    try:
        yield read
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_no_qc_read_never_initializes_store(app, ws, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Edit must not initialize QC before its journal exists")
    monkeypatch.setattr("vwb.qc.chat_store.QCStore", unexpected)
    for _ in range(2):
        snapshot = app.snapshot(ws)
        assert snapshot["qc_findings"] == []
        assert "qc_target" not in snapshot["outputs"][0]
    assert not (ws.root / ".qc-chat").exists()
    # A runtime folder without a finding journal is also insufficient.
    (ws.root / ".qc-chat").mkdir()
    assert app.snapshot(ws)["qc_findings"] == []
    assert not (ws.root / ".qc-chat/events.jsonl").exists()


def test_edit_api_exposes_only_sent_snapshot_and_not_later_private_changes(read_api, ws, movie):
    store = QCStore(ws)
    item = store.create(fixtures.payload())
    store.create(fixtures.payload(observed="UNHANDED_SHARED_SECRET"))
    store.create(fixtures.payload(lane="audit", observed="AUDIT_SECRET"))
    private = QCWorkspace(ws)
    private.initialize()
    private.add_chat("human", "QC_CHAT_SECRET")
    assert read_api()["qc_findings"] == []

    handoff(store, item)
    first = read_api()
    assert first["qc_findings"] == [item]
    assert first["outputs"][0]["qc_target"]["sha256"] == item["anchor"]["sha256"]
    assert first["outputs"][0]["qc_target"]["canvas_file_sha256"] == item["anchor"]["canvas_file_sha256"]

    fixtures.render(ws, movie, version=2)
    store.reconcile()
    current = store.get(item["id"])
    assert current["status"] == "needs_recheck"
    newer = store.verify_target("output:clip@v2")
    recheck = store.prepare_recheck(item["id"], newer["target"], f0=0, f1=6,
        pin={key: newer[key] for key in ("sha256", "canvas_file_sha256")},
        expected_revision=current["revision"], idempotency_key="private-recheck", model="fixture-model", provider="fixture")
    store.complete_recheck(recheck["id"], assessment="inconclusive", summary="PRIVATE_RECHECK_RESULT",
                           evidence=[], model="fixture-model", provider="fixture")
    store.transition(item["id"], "resolved", expected_revision=current["revision"], note="PRIVATE_RECHECK_SECRET")
    journal = (ws.root / ".qc-chat/events.jsonl").read_bytes()
    after = read_api()
    assert after["qc_findings"] == first["qc_findings"]
    assert after["qc_findings"][0]["anchor"]["version"] == 1
    assert after["qc_findings"][0]["status"] == "open"
    assert after["outputs"][0]["qc_target"]["target"] == "output:clip@v2"
    assert (ws.root / ".qc-chat/events.jsonl").read_bytes() == journal
    for secret in ("UNHANDED_SHARED_SECRET", "AUDIT_SECRET", "QC_CHAT_SECRET", "PRIVATE_RECHECK_SECRET",
                   "PRIVATE_RECHECK_RESULT", "needs_recheck", recheck["id"]):
        assert secret not in json.dumps(after)


def test_prepared_but_unsent_handoff_is_not_exposed(app, ws):
    store = QCStore(ws)
    item = store.create(fixtures.payload())
    def offline(**kwargs):
        raise RuntimeError("Queue offline")
    with pytest.raises(RuntimeError, match="offline"):
        store.handoff([item["id"]], offline, "pending")
    assert app.snapshot(ws)["qc_findings"] == []
    handoff(store, item, "pending")
    assert app.snapshot(ws)["qc_findings"] == [item]


@pytest.mark.parametrize("changed", ["render", "canvas", "receipt_file", "missing_render"])
def test_uncertified_displayed_render_retains_finding_without_seek_descriptor(app, ws, changed):
    store = QCStore(ws)
    item = store.create(fixtures.payload())
    handoff(store, item)
    video = ws.renders / "clip/v1/clip_v1.mp4"
    if changed == "render":
        video.write_bytes(b"different pixels")
    elif changed == "canvas":
        canvas = ws.canvas / "clip/v1.json"
        data = read_json(canvas)
        data["duration_frames"] += 1
        write_json(canvas, data)
    elif changed == "missing_render":
        video.unlink()
    else:
        alternate = video.with_name("alternate.mp4")
        shutil.copyfile(video, alternate)
        receipt = read_json(video.parent / "receipt.json")
        receipt["file"] = str(alternate)
        write_json(video.parent / "receipt.json", receipt)
        assert store.verify_target("output:clip@v1")["file"].endswith("alternate.mp4")
    snapshot = app.snapshot(ws)
    assert snapshot["qc_findings"] == [item]
    assert "qc_target" not in snapshot["outputs"][0]


def test_only_handed_outputs_current_versions_are_described_with_editor_audience(app, ws, movie, monkeypatch):
    store = QCStore(ws)
    item = store.create(fixtures.payload())
    handoff(store, item)
    fixtures.render(ws, movie, version=2)
    fixtures.render(ws, movie, output="unhanded")
    store.create(fixtures.payload(target="output:unhanded@v1"))
    calls = []
    describe, listing = QCStore.describe_target, QCStore.list_items
    def checked(self, target):
        calls.append(("describe", target))
        return describe(self, target)
    def listed(self, *, audience):
        calls.append(("audience", audience))
        return listing(self, audience=audience)
    monkeypatch.setattr(QCStore, "describe_target", checked)
    monkeypatch.setattr(QCStore, "list_items", listed)
    snapshot = app.snapshot(ws)
    assert calls == [("audience", "editor"), ("describe", "output:clip@v2")]
    assert snapshot["qc_findings"] == [item]
    assert snapshot["outputs"][0]["qc_target"]["version"] == 2
    assert snapshot["outputs"][1]["id"] == "unhanded"
    assert "qc_target" not in snapshot["outputs"][1]


def test_snapshot_reuses_cached_hash_but_changed_media_invalidates_it(app, ws, movie, monkeypatch):
    store = QCStore(ws)
    item = store.create(fixtures.payload())
    handoff(store, item)
    video = ws.renders / "clip/v1/clip_v1.mp4"
    original_stat = video.stat()
    # Count actual file bytes read, not calls to the descriptor/hash wrapper.
    # Begin cold even though finding creation and handoff have verified media.
    monkeypatch.setattr(chat_store, "_HASH_CACHE", OrderedDict())
    real_fdopen, reads = os.fdopen, []
    class TrackedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            data = self.stream.read(size)
            reads.append(len(data))
            return data

    def tracked_fdopen(fd, *args, **kwargs):
        info = os.fstat(fd)
        stream = real_fdopen(fd, *args, **kwargs)
        if (info.st_dev, info.st_ino) == (original_stat.st_dev, original_stat.st_ino):
            return TrackedReader(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", tracked_fdopen)
    first = app.snapshot(ws)
    assert first["outputs"][0]["qc_target"]["sha256"] == item["anchor"]["sha256"]
    assert sum(reads) == len(movie)
    count = len(reads)
    assert app.snapshot(ws)["outputs"] == first["outputs"]
    assert len(reads) == count  # Even the empty EOF read is avoided on a warm cache.

    # Same inode, length and mtime; ctime must still invalidate the cached bytes.
    with video.open("r+b") as stream:
        stream.write(bytes([movie[0] ^ 1]))
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    changed = app.snapshot(ws)
    assert sum(reads) == 2 * len(movie)
    assert "qc_target" not in changed["outputs"][0]
    assert changed["qc_findings"] == [item]  # Original handoff survives invalidation.


def test_damaged_private_journal_fails_closed_without_exposing_diagnostics(app, ws):
    store = QCStore(ws)
    item = store.create(fixtures.payload())
    handoff(store, item)
    with (ws.root / ".qc-chat/events.jsonl").open("a") as stream:
        stream.write('{"private":"PRIVATE_CORRUPT_RECORD"}')
    snapshot = app.snapshot(ws)
    assert snapshot["qc_findings"] == []
    assert "qc_target" not in snapshot["outputs"][0]
    assert "PRIVATE_CORRUPT_RECORD" not in json.dumps(snapshot)


def run_js(source):
    if NODE is None:
        pytest.skip("Node is optional for frontend checks")
    setup = r"""
import assert from 'node:assert/strict';
globalThis.window={React:{
 createElement:(type,props,...children)=>({type,props:{...props,children:children.flat(Infinity)}}),
 useState:initial=>[initial,()=>{}],useRef:initial=>({current:initial})},
 Eeva:{Button:'Button',Icon:'Icon'}};
const {findingSeekTime,editorFindings,editorVideoURL,editorMediaKey,seekFinding,EditorQCMarkers,EditorOutputPreview}=await import('./src/vwb/web/static/editor-qc-markers.js');
const target={target:'output:clip@v1',output:'clip',version:1,fps:30,frames:120,
 file:'renders/clip/v1/clip_v1.mp4',sha256:'a'.repeat(64),canvas_file_sha256:'b'.repeat(64),url:'/files/r/renders/clip/v1/clip_v1.mp4'};
const output={id:'clip',version:1,video_url:target.url,qc_target:target};
const item={id:'sent',lane:'shared',observed:'Clipped title',expected:'Whole title',status:'open',anchor:{...target,f0:30,f1:45}};
const copy=value=>JSON.parse(JSON.stringify(value));
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
const video=()=>({readyState:1,error:null,currentSrc:'http://localhost'+editorVideoURL(output),
 getAttribute:()=>editorVideoURL(output),duration:4,currentTime:0,pause(){this.paused=true;}});
"""
    result = subprocess.run([NODE, "--input-type=module", "-e", setup + source],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_exact_pin_seek_uses_inside_frame_and_actual_loaded_duration():
    run_js(r"""
const v=video();assert.equal(seekFinding(v,item,output),true);assert.equal(v.paused,true);
assert.equal(Math.floor(v.currentTime*target.fps),30);
assert.ok(v.currentTime>1 && v.currentTime<31/30);
for(const fps of [24,29.97,30,60,120])for(const frame of [0,1,30,119]){
 const out={...output,qc_target:{...target,fps}};
 const finding={...item,anchor:{...out.qc_target,f0:frame,f1:frame+1}};
 const duration=119.8/fps, time=findingSeekTime(finding,out,duration);
 assert.ok(time>frame/fps && time<Math.min((frame+1)/fps,duration));
 assert.equal(Math.floor(time*fps),frame);
}
for(const duration of [0,NaN,Infinity,-1,null,'4',1])assert.equal(findingSeekTime(item,output,duration),null);
for(const patch of [{f0:-1},{f0:1.5},{f0:'30'},{f1:30},{f1:121},{f1:null}])
 assert.equal(findingSeekTime({...item,anchor:{...item.anchor,...patch}},output,4),null);
// A cached UI duration cannot authorize a seek after the media duration changes.
v.duration=1;v.currentTime=0;assert.equal(seekFinding(v,item,output),false);assert.equal(v.currentTime,0);
""")


def test_version_hash_and_descriptor_mismatches_never_seek():
    run_js(r"""
for(const patch of [{target:'output:clip@v2'},{version:2},{output:'other'},{sha256:'c'.repeat(64)},
 {canvas_file_sha256:'c'.repeat(64)},{file:'renders/clip/v1/alternate.mp4'},{fps:24},{frames:100}]){
 assert.equal(findingSeekTime({...item,anchor:{...item.anchor,...patch}},output,4),null);
}
for(const qc_target of [null,{...target,url:'/wrong.mp4'},{...target,sha256:''},{...target,canvas_file_sha256:'bad'},
 {...target,version:2},{...target,file:'renders/clip/v1/alternate.mp4'},{...target,fps:0},{...target,frames:0}]){
 assert.equal(findingSeekTime(item,{...output,qc_target},4),null);
}
for(const patch of [{readyState:0},{error:{}},{currentSrc:'http://localhost/other.mp4'},
 {getAttribute:()=>'/old.mp4'},{currentSrc:''}])assert.equal(seekFinding({...video(),...patch},item,output),false);
const rerender={...output,qc_target:{...target,sha256:'c'.repeat(64)}};
assert.notEqual(editorVideoURL(rerender),editorVideoURL(output));
assert.notEqual(editorMediaKey(rerender),editorMediaKey(output));
assert.equal(seekFinding(video(),{...item,anchor:{...item.anchor,sha256:'c'.repeat(64)}},rerender),false);
""")


def test_older_versions_are_separate_keep_original_version_and_have_no_seek_controls():
    run_js(r"""
const newer={...target,target:'output:clip@v2',version:2,file:'renders/clip/v2/clip_v2.mp4',
 url:'/files/r/renders/clip/v2/clip_v2.mp4',canvas_file_sha256:'c'.repeat(64)};
const current={...output,version:2,video_url:newer.url,qc_target:newer};
assert.notEqual(findingSeekTime({...item,anchor:{...newer,f0:30,f1:45}},current,4),null);
const groups=editorFindings([item],current,4);
assert.equal(groups.current.length,0);assert.equal(groups.older.length,1);assert.equal(groups.older[0].time,null);
let calls=0;const view=EditorQCMarkers({findings:[item],output:current,duration:4,onSeek:()=>calls++});
assert.match(words(view),/Older-version findings/);assert.match(words(view),/Version\s+1/);
assert.ok(!walk(view).some(n=>n.type==='button'||n.type==='Button'));
assert.ok(!walk(view).some(n=>n.props.className==='editor-qc-timeline'));
assert.equal(item.anchor.version,1);assert.equal(calls,0);
""")


def test_malicious_mixed_lanes_and_other_outputs_are_ignored():
    run_js(r"""
const mixed=[null,{},item,...['audit','private','editor',null].map(lane=>({...item,id:String(lane),lane,observed:'SECRET_'+lane})),
 {...item,id:'other',observed:'SECRET_other',anchor:{...item.anchor,target:'output:other@v1',output:'other'}},
 {...item,id:'forged',observed:'SECRET_forged',anchor:{...item.anchor,target:'output:other@v1'}}];
const groups=editorFindings(mixed,output,4);assert.deepEqual(groups.current.map(row=>row.item.id),['sent']);
const view=EditorQCMarkers({findings:mixed,output,duration:4,onSeek(){}});
assert.ok(!words(view).includes('SECRET'));assert.match(words(view),/Clipped title/);
assert.equal(EditorQCMarkers({findings:mixed.filter(i=>i!==item),output,duration:4}),null);
assert.deepEqual(editorFindings({},output,4),{current:[],older:[],other:[]});
""")


def test_marker_buttons_are_accessible_and_wait_for_metadata():
    run_js(r"""
const calls=[];
const waiting=EditorQCMarkers({findings:[item],output,onSeek:f=>calls.push(f)});
assert.ok(!walk(waiting).some(n=>n.type==='button'||n.type==='Button'));
assert.match(words(waiting),/Seek unavailable/);
const view=EditorQCMarkers({findings:[item],output,duration:4,onSeek:f=>calls.push(f)});
const nodes=walk(view), marker=nodes.find(n=>n.props.className==='editor-qc-marker');
assert.equal(marker.props['aria-label'],'Seek to QC finding sent, version 1, frames 30–44');
assert.equal(marker.props.style.left,'25%');assert.equal(marker.props.style.width,'12.5%');
marker.props.onClick();nodes.find(n=>n.type==='Button').props.onClick();assert.deepEqual(calls,[item,item]);
const preview=walk(EditorOutputPreview({output,findings:[item]}));
const media=preview.find(n=>n.type==='video');
assert.equal(media.props.src,editorVideoURL(output));assert.equal(typeof media.props.onLoadedMetadata,'function');
assert.equal(typeof media.props.onDurationChange,'function');assert.equal(typeof media.props.onEmptied,'function');
assert.equal(typeof media.props.onError,'function');
assert.ok(!preview.some(n=>n.props.className==='editor-qc-timeline'));
""")
