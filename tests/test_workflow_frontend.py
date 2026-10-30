"""Workflow composer contracts with synthetic API data and optional browser checks."""

import os
import subprocess

import pytest
from test_steering_frontend import NODE, ROOT, run_js

pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_workflow_js(source):
    run_js(r"""
const workflow={id:'brand-context',name:'Build brand context',description:'Build project-shared brand memory',command:'/brand-context'};
const folder={id:'Brand assets',name:'work/Brand assets/',kind:'folder'};
const catalog={workflows:[workflow],folders:[folder],skills:[{id:'remake',name:'Remake'}],files:[]};
snapshot={...snapshot,request:{id:'r-test',project_id:'p-test',chat:[]},asks:[],active:null};
let requestRows=[{id:'r-test',title:'Test',project_id:'p-test'},{id:'r-other',title:'Other',project_id:null}];
let failed=false,holdSubmission=null;
globalThis.setTimeout=(callback,ms)=>{if(ms<=180)Promise.resolve().then(callback);return 1;};
globalThis.CustomEvent=class {constructor(type,options){this.type=type;this.detail=options.detail;}};
window.dispatchEvent=()=>{};
globalThis.fetch=async(url,options={})=>{
 const body=typeof options.body==='string'?JSON.parse(options.body):options.body;
 calls.push({url,options,body});
 if(url.endsWith('/project')){snapshot.request.project_id=body.project_id;requestRows[0].project_id=body.project_id;}
 if(url.endsWith('/messages')&&holdSubmission)await holdSubmission;
 const result=url==='/api/bootstrap'?{csrf_token:'test'}:
  url==='/api/requests'?{requests:requestRows,projects:[{id:'p-test',name:'Test project'}]}:
  url==='/api/workspaces'?{request_id:'r-test'}:
  /\/features(?:\?|$)/.test(url)?catalog:
  url==='/api/uploads'?{upload:{id:'upload-test',name:'notes.txt',kind:'text'}}:
  url.includes('/imports')?{jobs:[]}:
  url.endsWith('/messages')?(failed?{error:'Synthetic server validation failed'}:{job:{id:'queued'}}):
  url.endsWith('/answers')?{delivery:'waiting_tool'}:
  url.startsWith('/api/requests/r-other')?{request:{id:'r-other',project_id:null,chat:[]},asks:[],events:[],queue:[]}:
  snapshot;
 return {ok:!(failed&&url.endsWith('/messages')),json:async()=>result};
};
const {ComposerControls}=await import('../../src/vwb/web/static/composer-controls.js');
const {ProjectNavigation}=await import('../../src/vwb/web/static/conversation-controls.js');
const {LocalImportDialog}=await import('../../src/vwb/web/static/local-import-control.js');
const button=()=>all(view).find(node=>node.props?.['aria-label']==='Workflows');
const shell=()=>all(view).find(node=>node.props?.className==='composer-shell');
const navigation=()=>all(view).find(node=>node.type===ProjectNavigation);
const controls=()=>all(view).find(node=>node.type===ComposerControls);
const selectedChips=()=>words(all(view).find(node=>node.props?.className==='reference-chips'));
const guidance=()=>words(composer().props.attachments);
const type=value=>{composer().props.onValueChange(value);render();};
const settle=async()=>{await flush();render();await flush();render();};
const pickOption=async kind=>{
 await settle();const option=all(view).find(node=>node.props?.role==='option'&&(kind==='workflow'?words(node).includes('Build brand context'):kind==='folder'?words(node).includes('Brand assets'):words(node).includes('Remake')));
 assert.ok(option,`Missing ${kind} option`);option.props.onClick();render();await settle();
};
const chooseWorkflow=async()=>{button().props.onClick();render();await pickOption('workflow');};
const chooseFolder=async()=>{type('@Brand');await pickOption('folder');};
const remove=label=>{all(view).find(node=>node.props?.label===`Remove ${label}`).props.onClick();render();};
""" + source)


def test_workflow_triggers_matching_and_context_preserve_other_reference_types():
    run_workflow_js(r"""
const {triggerAt,matchingOptions,toContext,workflowReferences}=await import('../../src/vwb/web/static/composer-picker.js');
for(const text of ['/brand-context','/b','/brand-con']){
 assert.equal(triggerAt(text).kind,'workflow');
 assert.equal(matchingOptions(catalog,triggerAt(text))[0].id,'brand-context');
}
assert.deepEqual(triggerAt('Use /workflows brand'),{kind:'workflow',query:'brand',start:4,end:20});
assert.equal(matchingOptions(catalog,triggerAt('/workflows shared'))[0].kind,'workflow');
assert.equal(matchingOptions(catalog,triggerAt('/workflows'),[{...workflow,kind:'workflow'}]).length,0);
for(const value of ['/workflowssss','https://example.test/brand-context','path/brand','/unknown','/brand-contextual','email@brand'])assert.equal(triggerAt(value),null,value);
assert.equal(triggerAt('/features remake').kind,'skill');
assert.equal(triggerAt('@"work/Brand assets"').kind,'path');
const selected=[folder,{kind:'file',id:'notes.txt'},{kind:'skill',id:'remake'},{kind:'plugin',id:'design'}];
const context=toContext(workflowReferences('/brand-context use logos',selected));
assert.deepEqual(context,{skills:['remake'],folders:['Brand assets'],plugins:['design'],files:['notes.txt'],workflows:['brand-context']});
assert.ok(!('workflows' in toContext(selected)));
assert.equal(workflowReferences('/brand-contextual',selected),selected);
assert.equal(workflowReferences('Explain /brand-context',selected),selected);
const direct=workflowReferences('/brand-context',selected);
assert.equal(workflowReferences('/brand-context',direct),direct);
""")


@pytest.mark.parametrize("delivery", ["queue", "now", "paused"])
def test_workflow_and_folder_send_without_prose_and_without_canvas_instructions(delivery):
    run_workflow_js(f"const delivery='{delivery}';" + r"""
if(delivery==='paused')snapshot.paused=true;else snapshot.active={id:'active',can_stop:true};
await mount();controls().props.onChange({mode:'agentic',renderers:['after_effects','browser']});render();
await chooseWorkflow();assert.equal(controls(),undefined);
assert.ok(selectedChips().includes('Workflow: Build brand context'));
assert.equal(composer().props.canSend,false);assert.ok(guidance().includes('Tag a folder'));
await chooseFolder();assert.equal(composer().props.value,'');assert.equal(composer().props.canSend,true);
// Eeva requires a truthy attachments slot as well as canSend for an empty textarea.
assert.ok(composer().props.attachments);assert.ok(guidance().includes('extra message is optional'));
if(delivery==='now')await SteeringAction(steering().props).props.onClick();
else await composer().props.onSend('');
await settle();const sent=submissions().at(-1);
assert.deepEqual(sent.body,{message:'/brand-context',mode:delivery==='queue'?'queue':'now',attachments:[],context:{skills:[],folders:['Brand assets'],plugins:[],workflows:['brand-context']}});
assert.equal(selectedChips(),'');assert.deepEqual(controls().props.preference.renderers,['browser','after_effects']);
""")


def test_missing_project_and_folder_block_both_send_paths_and_keep_the_draft():
    run_workflow_js(r"""
snapshot.request.project_id=null;snapshot.active={id:'active',can_stop:true};
await mount();type('Keep this instruction');await attach();await chooseWorkflow();
assert.ok(guidance().includes('Assign this conversation to a project'));
assert.ok(guidance().includes('Tag a folder'));assert.ok(guidance().includes('import a folder first'));
assert.equal(composer().props.canSend,false);assert.equal(SteeringAction(steering().props).props.disabled,true);
await composer().props.onSend(composer().props.value);await SteeringAction(steering().props).props.onClick();
assert.equal(submissions().length,0);assert.equal(composer().props.value,'Keep this instruction');
assert.ok(guidance().includes('notes.txt'));await chooseFolder();assert.equal(composer().props.canSend,false);
// An explicit null snapshot wins over stale list membership; assignment refreshes both.
await navigation().props.onMoveConversation('r-test','p-test');await settle();
assert.equal(composer().props.canSend,true);assert.ok(selectedChips().includes('Build brand context'));
await composer().props.onSend(composer().props.value);await settle();
assert.deepEqual(submissions().at(-1).body.attachments,['upload-test']);
assert.deepEqual(submissions().at(-1).body.context.workflows,['brand-context']);
""")


def test_import_creates_workspace_without_resetting_workflow_or_prose():
    run_workflow_js(r"""
requestRows=[];snapshot.request.project_id=null;
await mount();await chooseWorkflow();type('Use the campaign materials');
const dialog=()=>all(view).find(node=>node.type===LocalImportDialog);
const id=await dialog().props.ensureWorkspace();await settle();
assert.equal(id,'r-test');assert.equal(composer().props.value,'Use the campaign materials');
dialog().props.onReady(folder,id,{files:3});await settle();
assert.ok(selectedChips().includes('Build brand context'));assert.ok(selectedChips().includes('@work/Brand assets/'));
assert.equal(composer().props.canSend,false);assert.ok(guidance().includes('Assign this conversation'));
assert.ok(!guidance().includes('Tag a folder'));
assert.equal(submissions().length,0);
""")


def test_workflow_drafts_failures_direct_commands_and_navigation():
    run_workflow_js(r"""
delete snapshot.request.project_id; // Legacy snapshots fall back to request list membership.
await mount();type('Preserve this brief');await chooseWorkflow();
assert.equal(composer().props.value,'Preserve this brief');
await chooseFolder();type('Preserve this brief');await attach();
navigation().props.onChoose('r-other');await settle();assert.equal(selectedChips(),'');
navigation().props.onChoose('r-test');await settle();assert.ok(selectedChips().includes('Build brand context'));
assert.equal(composer().props.value,'Preserve this brief');assert.ok(guidance().includes('notes.txt'));
failed=true;await composer().props.onSend(composer().props.value);await settle();
assert.ok(selectedChips().includes('Build brand context'));assert.equal(composer().props.value,'Preserve this brief');
assert.ok(words(view).includes('Synthetic server validation failed'));assert.ok(guidance().includes('notes.txt'));
remove('Build brand context');assert.ok(!selectedChips().includes('Build brand context'));assert.ok(controls());
type('/brand-context use the tagged folder');assert.equal(composer().props.canSend,true);
failed=false;await composer().props.onSend(composer().props.value);await settle();
assert.equal(submissions().at(-1).body.message,'/brand-context use the tagged folder');
assert.deepEqual(submissions().at(-1).body.context.workflows,['brand-context']);
""")


def test_answers_qc_and_history_keep_workflows_in_edit_instructions():
    run_workflow_js(r"""
snapshot.asks=[ticket];snapshot.active={id:'active',can_stop:true};
snapshot.request.chat=[{id:'human',role:'human',ts:'2026-01-01T00:00:00Z',text:'/brand-context',context:{workflows:['brand-context'],folders:['Brand assets']},attachments:[{id:'saved',name:'saved.txt'}]}];
snapshot.queue=[{id:'queued',status:'queued',message:'/brand-context',context:{workflows:['brand-context']},attachment_refs:[{id:'queued-attachment',name:'queued.txt'}]}];
await mount();assert.equal(button(),undefined);purpose('New instruction');await chooseWorkflow();await chooseFolder();
purpose('Answer question');assert.equal(button(),undefined);
composer().props.question.props.setAnswer('deny');render();await composer().props.onSend('');await settle();
assert.ok(submissions().at(-1).url.endsWith('/answers'));assert.ok(!('context' in submissions().at(-1).body));
purpose('New instruction');assert.ok(selectedChips().includes('Build brand context'));
const badges=all(view).filter(node=>node.type?.name==='ReferenceTags').map(node=>words(node.type(node.props)));
assert.ok(badges.every(text=>text.includes('/brand-context')));assert.equal(badges.length,2);
const attachments=all(view).filter(node=>node.type?.name==='ChatAttachments');
assert.deepEqual(attachments.map(node=>node.props.items[0].id),['saved','queued-attachment']);
const tabs=all(view).find(node=>node.props?.className==='chat-mode-tabs');tabs.props.onValueChange('qc');render();render();
assert.equal(button(),undefined);assert.equal(composer().props.canSend,false);
const count=submissions().length;await composer().props.onSend('/brand-context');assert.equal(submissions().length,count);
type('/brand-context');assert.equal(all(view).some(node=>node.props?.role==='listbox'),false);
tabs.props.onValueChange('edit');render();assert.ok(selectedChips().includes('Build brand context'));
""")


def test_successful_submission_keeps_new_text_and_references_entered_while_saving():
    run_workflow_js(r"""
await mount();await chooseWorkflow();await chooseFolder();
let release;holdSubmission=new Promise(resolve=>release=resolve);
const sending=composer().props.onSend('');await flush();render();
type('Next instruction');shell().props.onDrop({preventDefault(){},dataTransfer:{files:[],getData:()=>JSON.stringify({requestId:'r-test',path:'work/Next assets',kind:'directory'})}});render();
release();await sending;await settle();
assert.equal(composer().props.value,'Next instruction');assert.ok(selectedChips().includes('Next assets'));
assert.ok(!selectedChips().includes('Build brand context'));assert.ok(!selectedChips().includes('Brand assets'));
""")


@pytest.mark.skipif(os.environ.get("VWB_UI_SMOKE") != "1", reason="Opt-in local Chromium UI check")
@pytest.mark.parametrize("width", [1280, 484])
def test_accessible_workflow_picker_in_real_app_with_synthetic_routes(tmp_path, width):
    source = r"""
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
import {chromium} from 'playwright-core';
import {findChromium} from './browser-environment.mjs';
const root=resolve('../../src/vwb/web/static');
const workflow={id:'brand-context',name:'Build brand context',description:'A subagent builds shared brand memory from a tagged folder.',command:'/brand-context'};
const folder={id:'Brand assets',name:'work/Brand assets/',description:'Folder in this workspace'};
let project=null,catalogError=false,sent=[];
const request=()=>({id:'r-fixture',project_id:project,message:'Synthetic brief',created:'2026-01-01T00:00:00Z',sources:[],chat:sent.map((body,i)=>({id:`human-${i}`,role:'human',ts:`2026-01-01T00:00:0${i}Z`,text:body.message,context:body.context,attachments:[]}))});
const snapshot=()=>({request:request(),asks:[],events:[],queue:[],active:null,outputs:[],cursor:0});
const browser=await chromium.launch({headless:true,executablePath:await findChromium()});
let page;
try{
 page=await browser.newPage({viewport:{width:Number(process.env.WORKFLOW_TEST_WIDTH),height:900}});
 page.setDefaultTimeout(7000);
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());assert.equal(url.hostname,'workflow.test');
  const path=url.pathname;
  if(path.startsWith('/api/')){
   if(req.method()==='PUT'){
    assert.equal(path,'/api/requests/r-fixture/code/context');return route.fulfill({json:{}});
   }
   if(req.method()==='POST'){
    assert.equal(path,'/api/requests/r-fixture/messages');sent.push(req.postDataJSON());
    return route.fulfill({json:{job:{id:'queued'}}});
   }
   assert.equal(req.method(),'GET');
   if(path.endsWith('/features')&&catalogError)return route.fulfill({status:503,json:{error:'Synthetic catalog unavailable'}});
   const result=path==='/api/bootstrap'?{csrf_token:'test'}:
    path==='/api/models'?{models:[],providers:[],default_selection:{}}:
    path==='/api/prompts'?{profiles:[],default_profile_id:null}:
    path==='/api/requests'?{requests:[{id:'r-fixture',title:'Synthetic conversation',project_id:project}],projects:[{id:'p-fixture',name:'Fixture project'}]}:
    path.endsWith('/features')?{workflows:[workflow],folders:[folder],skills:[{id:'remake',name:'Remake',description:'Skill fixture'}],files:[]}:
    path.endsWith('/imports')?{jobs:[]}:
    path.endsWith('/qc')?{messages:[],items:[],asks:[],queue:[]}:
    /^\/api\/requests\/r-fixture(?:\/events)?$/.test(path)?snapshot():{};
   return route.fulfill({json:result});
  }
  assert.equal(req.method(),'GET');
  const file=resolve(root,'.'+(path==='/'?'/index.html':path));assert.ok(file.startsWith(root+'/'));
  const contentType={'.html':'text/html','.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.woff2':'font/woff2'}[extname(file)]||'application/octet-stream';
  await route.fulfill({contentType,body:await readFile(file)});
 });
 await page.goto('http://workflow.test/#r-fixture');
 const entry=page.getByRole('button',{name:'Workflows',exact:true});
 const input=page.getByRole('textbox',{name:'Message the agent',exact:true});
 const send=page.getByRole('button',{name:'Send',exact:true});
 const search=page.getByRole('combobox',{name:'Search workflows'});
 const list=page.getByRole('listbox',{name:'Choose a workflow'});
 const option=page.getByRole('option',{name:/Build brand context/});
 const open=async()=>{await entry.click();await list.waitFor();await option.waitFor();};
 const focused=locator=>locator.evaluate(node=>document.activeElement===node);
 const waitFocused=async locator=>{await page.waitForFunction(node=>document.activeElement===node,await locator.elementHandle());};
 await input.fill('Keep my draft');await open();assert.equal(await focused(search),true);
 assert.equal(await entry.getAttribute('aria-expanded'),'true');
 await search.fill('nothing matches');await page.getByText('No matching workflows. Try another search.',{exact:true}).waitFor();
 await search.fill('brand');await option.waitFor();await page.keyboard.press('ArrowDown');
 const active=await search.getAttribute('aria-activedescendant');assert.equal(active,await option.getAttribute('id'));
 const bounds=await page.locator('.reference-dropup').boundingBox();assert.ok(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=Number(process.env.WORKFLOW_TEST_WIDTH));
 await page.screenshot({path:process.env.WORKFLOW_TEST_SCREENSHOT,fullPage:true});
 await page.keyboard.press('Escape');await list.waitFor({state:'detached'});assert.equal(await focused(entry),true);
 assert.equal(await input.inputValue(),'Keep my draft');
 await open();await page.keyboard.press('Enter');await waitFocused(input);assert.equal(await focused(input),true);
 assert.equal(await input.inputValue(),'Keep my draft');assert.equal(sent.length,0);
 await page.getByRole('button',{name:'Remove Build brand context',exact:true}).waitFor();
 assert.equal(await send.isDisabled(),true);assert.ok((await page.locator('.workflow-guidance').innerText()).includes('Assign this conversation'));
 await input.fill('@Brand');await page.getByRole('option',{name:/work\/Brand assets/}).click();
 assert.equal(await input.inputValue(),'');assert.equal(await send.isDisabled(),true);
 // Synthetic membership refresh must unlock without discarding either selected reference.
 project='p-fixture';await send.waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.querySelector('button[aria-label="Send"]').disabled);
 await input.press('Enter');await page.waitForFunction(()=>document.querySelector('.message-references'));
 assert.equal(sent.length,1);assert.equal(sent[0].message,'/brand-context');
 assert.deepEqual(sent[0].context.workflows,['brand-context']);assert.deepEqual(sent[0].context.folders,['Brand assets']);
 assert.ok((await page.locator('.message-references').innerText()).includes('/brand-context'));
 // Prefix autocomplete consumes the command without sending; @ and /features still work.
 await input.fill('/brand-con');await option.waitFor();assert.equal(await input.getAttribute('aria-controls'),'composer-reference-options');
 await input.press('Enter');assert.equal(await input.inputValue(),'');assert.equal(sent.length,1);
 await page.getByRole('button',{name:'Remove Build brand context',exact:true}).click();
 await input.fill('/workflows brand');await option.waitFor();await input.press('Tab');
 await page.getByRole('button',{name:'Remove Build brand context',exact:true}).click();
 await input.fill('/features remake');await page.getByRole('option',{name:/Remake/}).click();
 await page.getByRole('button',{name:'Remove Remake',exact:true}).click();
 await input.fill('Draft for later');
 await page.getByRole('tab',{name:'QC',exact:true}).click();assert.equal(await entry.count(),0);
 assert.equal(await page.getByRole('option',{name:/Build brand context/}).count(),0);
 await page.getByRole('tab',{name:'Edit',exact:true}).click();assert.equal(await input.inputValue(),'Draft for later');
 catalogError=true;await entry.click();await page.getByText('Synthetic catalog unavailable',{exact:true}).waitFor();
 await search.press('Escape');assert.equal(await focused(entry),true);assert.equal(await input.inputValue(),'Draft for later');
 assert.deepEqual(errors,[]);
}catch(error){throw new Error(`${error.stack}\nFixture UI: ${await page.locator('body').innerText()}`);}
finally{await browser.close();}
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source], cwd=ROOT / "generators/remotion",
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "WORKFLOW_TEST_WIDTH": str(width),
             "WORKFLOW_TEST_SCREENSHOT": str(tmp_path / "workflow-picker.png")},
    )
    assert result.returncode == 0, result.stderr
