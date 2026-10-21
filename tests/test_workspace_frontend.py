"""Behavioral checks for canvas state and integration context; no browser mocks."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    result = subprocess.run([NODE, "--input-type=module", "-e", source],
                            cwd=ROOT / "generators/remotion", capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_canvas_groups_deduplicate_preserve_and_close_tabs():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window = {React:{createElement(){}}, Eeva:{}};
const {initialCanvas,canvasReducer:reduce,fileTab} = await import('../../src/vwb/web/static/workspace-canvas.js');
const original=initialCanvas(); const preserved=JSON.stringify(original);
const file=fileTab({path:'work/hello.html'});
let state=reduce(original,{type:'open',tab:file});
assert.equal(JSON.stringify(original),preserved);
assert.equal(state.groups[0].active,'file:work/hello.html');
state=reduce(state,{type:'split'});
state=reduce(state,{type:'open',tab:file});
assert.equal(state.focused,'primary');
assert.equal(state.groups.flatMap(g=>g.tabs).filter(t=>t.id===file.id).length,1);
state=reduce(state,{type:'move',id:file.id});
assert.equal(state.focused,'secondary'); assert.equal(state.groups[0].active,'outputs');
state=reduce(state,{type:'open',tab:fileTab({path:'assets/image.png'})});
state=reduce(state,{type:'close',id:'file:assets/image.png'});
assert.equal(state.groups[1].active,file.id);
state=reduce(state,{type:'join'});
assert.equal(state.groups.length,1); assert.equal(state.groups[0].tabs.length,2);
state=reduce(state,{type:'close',id:'outputs'});
assert.equal(state.groups[0].active,file.id);
state=reduce(state,{type:'close',id:file.id}); assert.equal(state.groups[0].active,null);
assert.equal(initialCanvas().groups[0].tabs.length,1);
""")


def test_directory_pages_and_import_event_replay():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window = {React:{createElement(){}}, Eeva:{}};
const {mergeDirectoryPage,importedTabs} = await import('../../src/vwb/web/static/workspace-canvas.js');
const previous={entries:[{path:'work/a',size:1}],next_cursor:'a'};
const page={entries:[{path:'work/a',size:2},{path:'work/b',size:3}],next_cursor:null};
const combined=mergeDirectoryPage(previous,page,true);
assert.equal(combined.entries.length,2); assert.equal(combined.entries[0].size,2);
assert.equal(previous.entries[0].size,1); assert.equal(combined.next_cursor,null);
assert.equal(mergeDirectoryPage(previous,page),page);
const event={seq:8,kind:'integration.imported',data:{artifacts:[
 {kind:'image',workspace_path:'work/integrations/one.png'},
 {kind:'image',workspace_path:'work/integrations/two.png'},
 {kind:'document',workspace_path:'work/integrations/response.json',mime_type:'application/json'}]}};
assert.deepEqual(importedTabs([event],undefined),{cursor:8,tabs:[]});
const fresh=importedTabs([event],7);
assert.deepEqual(fresh.tabs.map(t=>t.path),['work/integrations/one.png','work/integrations/response.json']);
assert.equal(importedTabs([event],fresh.cursor).tabs.length,0);
assert.equal(importedTabs([{...event,data:{...event.data,is_error:true}}],7).tabs.length,0);
assert.equal(importedTabs([{...event,kind:'tool.called'}],7).tabs.length,0);
""")


def test_plugin_context_and_public_settings_keep_actual_state():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window = {React:{createElement(){}}, Eeva:{}};
const {toContext} = await import('../../src/vwb/web/static/composer-picker.js');
const {publicIntegrationConfig,integrationState} = await import('../../src/vwb/web/static/integrations-control.js');
assert.deepEqual(toContext([{kind:'plugin',id:'figma'},{kind:'skill',id:'remake'},{kind:'folder',id:'assets'}]),
 {skills:['remake'],folders:['assets'],plugins:['figma']});
assert.deepEqual(publicIntegrationConfig({gateway_url:' https://example.test ',client_id:' id ',redirect_uri:' http://127.0.0.1/callback ',read_tools:'a\nb,c',secret:'not copied'}),
 {gateway_url:'https://example.test',client_id:'id',redirect_uri:'http://127.0.0.1/callback',read_tools:['a','b','c']});
assert.equal(integrationState({state:'unavailable',available:false}),'Unavailable');
assert.equal(integrationState({state:'disconnected',available:true}),'Not connected');
assert.equal(integrationState({}),'Not reported');
""")


def test_import_events_scan_long_history_without_spreading_function_arguments():
    run_js(r"""
import assert from 'node:assert/strict';
globalThis.window = {React:{createElement(){}}, Eeva:{}};
const {importedTabs} = await import('../../src/vwb/web/static/workspace-canvas.js');
const events = Array.from({length:150000}, (_,i) => ({seq:i+1,kind:'tool.called'}));
events[149999] = {seq:150000,kind:'integration.imported',data:{artifacts:[
 {kind:'image',workspace_path:'work/integrations/latest.png'}]}};
// Initial snapshots remain history even when they exceed an engine's argument cap.
assert.deepEqual(importedTabs(events,undefined),{cursor:150000,tabs:[]});
const fresh=importedTabs(events,149999);
assert.equal(fresh.cursor,150000);
assert.deepEqual(fresh.tabs.map(tab=>tab.path),['work/integrations/latest.png']);
assert.equal(importedTabs(events,fresh.cursor).tabs.length,0);
assert.equal(events.length,150000);
""")


def test_pane_resizing_fits_the_available_space_and_releases_hidden_panes():
    run_js(r"""
import assert from 'node:assert/strict';
const {paneLayout, panePreferences, COMPACT_WIDTH}=await import('../../src/vwb/web/static/pane-layout.js');
assert.equal(COMPACT_WIDTH,768);
for(const width of [768,800,1024,1280,1536,1920]) {
  for(const nav of [false,true]) for(const chat of [false,true]) {
    const p=paneLayout(width,{navigator:480,chat:2000},nav,chat);
    const total=52+(nav?p.navigator+8:0)+(chat?p.chat+12:0)+(width>=1536?20:12);
    assert.ok(width-total>=200,`canvas squeezed at ${width}`);
    assert.ok(p.navigator>=144 && p.chat>=280);
    assert.ok(p.navigator<=p.navigatorMax && p.chat<=p.chatMax);
  }
}
const a=paneLayout(1280,{navigator:196,chat:360},true,true);
const b=paneLayout(1280,{navigator:196,chat:520},true,true);
assert.equal(b.chat-a.chat,160); // Previously CSS silently capped this at 340.
const wider=paneLayout(1280,{navigator:320,chat:520},true,true);
assert.equal(wider.navigator,320); assert.equal(wider.chat,520);
assert.ok(paneLayout(800,{navigator:196,chat:600},false,true).chat > paneLayout(800,{navigator:196,chat:600},true,true).chat);
assert.ok(paneLayout(800,{navigator:480,chat:360},true,false).navigator > paneLayout(800,{navigator:480,chat:360},true,true).navigator);
assert.deepEqual(panePreferences({navigator:null,chat:Infinity}),{navigator:196,chat:360});
assert.deepEqual(panePreferences({navigator:240,chat:480}),{navigator:240,chat:480});
""")
