"""Video provider selection and live-catalog contracts; no external or paid calls."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Node is optional for frontend checks')


def run_js(source, *, env=None, timeout=20):
    result = subprocess.run([NODE, '--input-type=module', '-e', source],
                            cwd=ROOT / 'generators/remotion', capture_output=True, text=True,
                            timeout=timeout, env=env)
    assert result.returncode == 0, result.stderr + result.stdout


def test_video_payload_preserves_provider_custom_path_false_audio_and_zero_seed():
    run_js("""
import assert from 'node:assert/strict';
import {draftDefaults,requestBody,safeDraft} from '../../src/vwb/web/static/generation-geometry.js';
const legacy={...draftDefaults('video'),prompt:'A moving scene',model:'omni'};
assert.deepEqual(requestBody(legacy,'video','job'),{idempotency_key:'job',kind:'video',operation:'text',
 prompt:'A moving scene',model:'omni',inputs:[],provider:'google',seconds:6,resolution:'1080p',aspect:'9:16'});
const custom={...legacy,provider:'openrouter',model:'future-provider/new-video:variant',seconds:'',resolution:'',aspect:'',generate_audio:false,seed:'0'};
const payload=requestBody(custom,'video','job');
assert.equal(payload.model,custom.model);assert.equal(payload.provider,'openrouter');
assert.equal(payload.generate_audio,false);assert.equal(payload.seed,0);
for(const field of ['seconds','resolution','aspect'])assert.equal(payload[field],null);
const h3=requestBody({...custom,model:'minimax/hailuo-3',seconds:15,resolution:'2K',aspect:'1:1',generate_audio:true,seed:''},'video','h3');
assert.equal(h3.resolution,'2K');assert.equal(h3.aspect,'1:1');assert.equal(h3.seconds,15);assert.ok(!('seed' in h3));
for(const seed of ['-1','1.5','nope','2147483648','9007199254740992'])assert.throws(()=>requestBody({...custom,seed},'video','bad'),/seed/);
for(const seconds of [-1,0,1.5,601,'nope'])assert.throws(()=>requestBody({...custom,seconds},'video','bad'),/duration/);
assert.throws(()=>requestBody({...custom,model:' '},'video','bad'),/model path/);
assert.throws(()=>requestBody({...custom,generate_audio:'false'},'video','bad'),/audio/);
const image=requestBody({...draftDefaults('image'),prompt:'An image',model:'sunburst'},'image','image');
for(const field of ['provider','seconds','resolution','aspect','generate_audio','seed'])assert.ok(!(field in image));
assert.equal(safeDraft(custom).model,custom.model);assert.equal(safeDraft(custom).provider,'openrouter');
""")


def test_provider_switching_preserves_sources_operations_and_each_provider_settings():
    run_js("""
import assert from 'node:assert/strict';
import {draftDefaults,selectVideoProvider,videoProviders,videoProvider,videoOptions,videoSettingsError} from '../../src/vwb/web/static/generation-geometry.js';
const google={id:'google',label:'Google / Omni',models:[{id:'omni'}],operations:['text','frames','reference','edit','extend'],
 seconds:{min:3,max:10},resolutions:['720p','1080p'],aspects:['9:16','16:9'],available:true};
const cap={providers:[google,{id:'openrouter',available:true,default_model:'minimax/hailuo-3',operations:['text','frames','reference']}]};
const original={...draftDefaults('video'),model:'omni',operation:'extend',sources:[{path:'work/input.mp4',kind:'video'}],seconds:8};
const custom=selectVideoProvider(original,'openrouter',cap);
assert.equal(custom.model,'minimax/hailuo-3');assert.equal(custom.operation,'extend');assert.deepEqual(custom.sources,original.sources);
assert.equal(custom.seconds,'');assert.equal(custom.resolution,'');assert.equal(custom.aspect,'');
assert.match(videoSettingsError(custom,cap.providers[1],{id:custom.model}),/operation/);
const edited={...custom,model:'new/model',operation:'frames',seconds:5,resolution:'2K',aspect:'1:1'};
const back=selectVideoProvider(edited,'google',cap);assert.equal(back.model,'omni');assert.equal(back.seconds,8);assert.equal(back.resolution,'1080p');
const restored=selectVideoProvider(back,'openrouter',cap);assert.equal(restored.model,'new/model');assert.equal(restored.resolution,'2K');
assert.deepEqual(restored.sources,original.sources);assert.equal(restored.operation,'frames');
assert.equal(videoProviders(google)[0].id,'google');assert.equal(videoProvider(google).label,'Google / Omni');
assert.deepEqual(videoOptions(google).seconds,[3,4,5,6,7,8,9,10]);
assert.deepEqual(videoOptions(cap.providers[1],null),{seconds:[],resolutions:[],aspects:[],audio:false,seed:false,frames:[]});
""")


def test_catalog_options_are_model_specific_and_incompatible_inputs_are_explicit():
    run_js("""
import assert from 'node:assert/strict';
import {draftDefaults,videoOptions,videoSettingsError} from '../../src/vwb/web/static/generation-geometry.js';
const provider={id:'openrouter',operations:['text','frames','reference']};
const model={id:'minimax/hailuo-3',supported_durations:[5,6,15],supported_resolutions:['2K'],supported_aspect_ratios:['1:1','9:16'],
 supported_frame_images:['first_frame'],generate_audio:true,seed:false};
let draft={...draftDefaults('video'),provider:'openrouter',model:model.id,seconds:15,resolution:'2K',aspect:'1:1'};
assert.equal(videoSettingsError(draft,provider,model),'');
assert.match(videoSettingsError({...draft,resolution:'1080p'},provider,model),/not advertised/);
assert.match(videoSettingsError({...draft,seconds:10},provider,model),/Duration/);
assert.match(videoSettingsError({...draft,seed:'1'},provider,model),/seed/);
assert.match(videoSettingsError(draft,provider,null),/exact OpenRouter/);
assert.match(videoSettingsError({...draft,model:'another/model'},provider,model),/exact OpenRouter/);
assert.match(videoSettingsError(draft,undefined,model),/preserved/);
const source={kind:'image',path:'work/a.png'};
draft={...draft,operation:'frames',sources:[source]};assert.equal(videoSettingsError(draft,provider,model),'');
assert.match(videoSettingsError({...draft,sources:[source,source]},provider,model),/last-frame/);
assert.match(videoSettingsError({...draft,sources:[{kind:'video'}]},provider,model),/source type/);
assert.match(videoSettingsError({...draft,operation:'text'},provider,model),/No sources were removed/);
assert.equal(videoSettingsError({...draft,operation:'reference'},provider,model),''); // Unknown reference support goes to provider validation.
assert.match(videoSettingsError(draft,provider,{...model,supported_frame_images:null}),/first-frame/);
assert.match(videoSettingsError(draft,provider,{...model,supported_frame_images:[]}),/first-frame/);
assert.equal(videoOptions(provider,{generate_audio:'true',seed:1}).audio,false);
assert.equal(videoOptions(provider,{generate_audio:'true',seed:1}).seed,false);
""")


def test_image_studio_accepts_reused_local_cutout_with_null_model():
    run_js("""
import assert from 'node:assert/strict';
import {draftDefaults} from '../../src/vwb/web/static/generation-geometry.js';
const state=[{...draftDefaults('image'),model:null,operation:'remove_background'},
 {image:{available:false,models:[],operations:['remove_background'],background_methods:[{id:'matte',available:true}]}},[],null,'',false,false,false,'',false,0];
let index=0;
globalThis.window={React:{createElement(type,props,...children){return {type,props:{...props,children}};},
 useState(value){const at=index++;return [at<state.length?state[at]:typeof value==='function'?value():value,()=>{}];},
 useRef(current){return {current};},useEffect(){}},Eeva:{Button:'button',Icon:'i',Tooltip:'span',MessageBar:'aside',SolsticeMark:'svg'}};
const {GenerationStudio}=await import('../../src/vwb/web/static/generation-studio.js');
function text(node){if(node==null||typeof node==='boolean')return '';if(Array.isArray(node))return node.map(text).join(' ');
 if(typeof node!=='object')return String(node);if(typeof node.type==='function')return text(node.type(node.props));return text(node.props.children);}
assert.ok(text(GenerationStudio({kind:'image',requestId:'local-cutout',api(){throw Error('No API while rendering');}})).includes('Local matte'));
""")


@pytest.mark.skipif(os.environ.get('VWB_UI_SMOKE') != '1', reason='Opt-in local Chromium UI check')
@pytest.mark.parametrize('viewport', [(1280, 850), (390, 780)])
def test_video_studio_live_search_payloads_and_conversation_persistence(tmp_path, viewport):
    source = r"""
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
import {chromium} from 'playwright-core';
import {findChromium} from './browser-environment.mjs';
const root=resolve('../../src/vwb/web/static');
const fixture=`<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/vendor/eeva/tokens.css"><link rel="stylesheet" href="/generation-studio.css">
<script src="/vendor/react.production.min.js"></script><script src="/vendor/react-dom.production.min.js"></script>
<style>html,body,#root{margin:0;height:100%}button{cursor:pointer}</style></head><body><div id="root"></div><script type="module">
const h=React.createElement;
window.Eeva={Button:({variant,size,loading,iconOnly,pressed,...props})=>h('button',props),Icon:()=>null,
 Tooltip:({children})=>children,MessageBar:({children})=>h('aside',{},children),SolsticeMark:()=>null};
const {GenerationStudio}=await import('/generation-studio.js');
window.sent=[];window.queries=[];window.orAvailable=true;window.offline=false;window.fixtureJobs=[];
const google={id:'google',label:'Google / Omni',available:true,models:[{id:'omni',label:'Omni v1.1'}],
 operations:['text','frames','reference','edit','extend'],seconds:{min:3,max:10},resolutions:['720p','1080p'],aspects:['9:16','16:9']};
const h3={id:'minimax/hailuo-3',name:'MiniMax: H3',description:'A video model.',supported_durations:[5,6,7,8,9,10,11,12,13,14,15],
 supported_resolutions:['2K'],supported_aspect_ratios:['1:1','9:16','16:9'],supported_frame_images:['first_frame','last_frame'],generate_audio:true,seed:false};
const future={...h3,id:'future/new-model',name:'Future model',generate_audio:false,seed:true,supported_resolutions:['480p'],supported_frame_images:['first_frame']};
const pixel='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=';
const api=async(path,options={})=>{
 const url=new URL(path,location.href);
 if(url.pathname.endsWith('/capabilities'))return {image:{available:false,models:[],operations:[]},video:{...google,providers:[google,
  {id:'openrouter',label:'OpenRouter',available:window.orAvailable,custom_model:true,default_model:h3.id,operations:['text','frames','reference']}]}};
 if(url.pathname.endsWith('/video-models')){
  const query=url.searchParams.get('query');const call={query,signal:options.signal};window.queries.push(call);
  await new Promise(resolve=>setTimeout(resolve,query==='slow/model'?700:10));
  if(window.offline)throw Error('catalog offline');
  const items=query==='slow/model'?[{...future,id:query}]:[h3,future].filter(m=>m.id.includes(query)||m.name.toLowerCase().includes(query.toLowerCase()));
  return {provider:'openrouter',items,total:items.length,offset:0,next_offset:null};
 }
 if(url.pathname.endsWith('/workspace/file'))return {path:url.searchParams.get('path'),url:pixel,mime:'image/png'};
 if(url.pathname.endsWith('/jobs')&&options.method==='POST'){
  const input=JSON.parse(options.body);window.sent.push(input);return {id:'job-'+window.sent.length,kind:'video',status:'failed',error:'Fixture completed; no model was called.',input,
   provider_job:{provider:input.provider,model:input.model,id:'provider-job-123',status:'processing'}};
 }
 if(url.pathname.endsWith('/jobs'))return {jobs:window.fixtureJobs};
 if(url.pathname.includes('/jobs/'))return {id:url.pathname.split('/').at(-1),kind:'video',status:'failed',error:'Fixture job; no provider call.'};
 throw Error('Unexpected fixture request '+path);
};
function Fixture(){const [id,setId]=React.useState('one');window.navigate=setId;return h(GenerationStudio,{kind:'video',requestId:id,api});}
ReactDOM.createRoot(document.getElementById('root')).render(h(Fixture));
</script></body></html>`;
const browser=await chromium.launch({headless:true,executablePath:await findChromium()});
try{
 const page=await browser.newPage({viewport:{width:Number(process.env.STUDIO_WIDTH),height:Number(process.env.STUDIO_HEIGHT)}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());assert.equal(url.hostname,'studio.test');assert.equal(route.request().method(),'GET');
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fixture});
  const path=resolve(root,'.'+url.pathname);assert.ok(path.startsWith(root+'/'));
  await route.fulfill({contentType:{'.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.woff2':'font/woff2'}[extname(path)]||'application/octet-stream',body:await readFile(path)});
 });
 await page.goto('https://studio.test');
 const provider=page.getByLabel('Video provider'),model=page.getByLabel('OpenRouter model path'),duration=page.getByLabel('Duration'),resolution=page.getByLabel('Resolution'),aspect=page.getByLabel('Aspect');
 const generate=page.getByRole('button',{name:'Generate video',exact:true}),status=page.locator('.studio-video-model-status');
 const ready=async text=>{await page.waitForFunction(text=>document.querySelector('.studio-video-model-status')?.textContent.includes(text),text);};
 await provider.waitFor();assert.equal(await provider.inputValue(),'google');assert.equal(await duration.inputValue(),'6');
 await provider.selectOption('openrouter');await ready('MiniMax: H3');
 assert.equal(await model.inputValue(),'minimax/hailuo-3');assert.equal(await duration.inputValue(),'');assert.equal(await resolution.inputValue(),'');
 assert.deepEqual(await resolution.locator('option').allTextContents(),['Provider default','2K']);
 assert.equal(await page.getByLabel('Seed',{exact:true}).count(),0);
 await model.fill('H3');await ready('matching models');assert.equal(await generate.isDisabled(),true);
 assert.equal(await page.locator('datalist option').first().getAttribute('value'),'minimax/hailuo-3');
 await model.fill('minimax/hailuo-3');await ready('MiniMax: H3');
 await page.getByLabel('Describe your video').fill('A slow orbit around a sculpture');
 await duration.selectOption('15');await resolution.selectOption('2K');await aspect.selectOption('1:1');await page.getByLabel('Generate audio').selectOption('false');
 await generate.click();await page.waitForFunction(()=>window.sent.length===1);
 const payload=await page.evaluate(()=>window.sent[0]);
 assert.equal(payload.provider,'openrouter');assert.equal(payload.model,'minimax/hailuo-3');assert.equal(payload.resolution,'2K');
 assert.equal(payload.seconds,15);assert.equal(payload.aspect,'1:1');assert.equal(payload.generate_audio,false);assert.ok(!('seed' in payload));
 assert.ok((await page.locator('.studio-provider-job').innerText()).includes('provider-job-123'));
 assert.ok((await page.locator('.studio-provider-job').innerText()).includes('does not confirm'));
 // Preserve sources when a model only accepts one frame, or a new operation accepts none.
 await page.getByLabel('Operation',{exact:true}).selectOption('frames');
 for(const name of ['first.png','last.png']){
  await page.getByRole('button',{name:'From workspace',exact:true}).click();await page.getByLabel('Workspace file path').fill('work/'+name);
  await page.getByRole('button',{name:'Add file',exact:true}).click();await page.getByLabel('Workspace file path').waitFor({state:'detached'});
 }
 assert.equal(await page.locator('.studio-source').count(),2);
 await model.fill('future/new-model');await ready('Future model');assert.equal(await page.locator('.studio-source').count(),2);
 assert.equal(await page.getByLabel('Generate audio').count(),0);assert.equal(await generate.isDisabled(),true);
 await page.getByRole('button',{name:'Reset video options'}).click();assert.ok((await page.locator('.studio-video-validation').innerText()).includes('last-frame'));
 await page.getByLabel('Operation',{exact:true}).selectOption('text');assert.equal(await page.locator('.studio-source').count(),2);
 for(let i=0;i<2;i++)await page.getByRole('button',{name:'Remove source',exact:true}).first().click();
 await page.getByLabel('Seed',{exact:true}).fill('0');await generate.click();await page.waitForFunction(()=>window.sent.length===2);
 const next=await page.evaluate(()=>window.sent[1]);assert.equal(next.model,'future/new-model');assert.equal(next.seed,0);
 for(const name of ['seconds','resolution','aspect'])assert.equal(next[name],null);
 assert.ok(!('generate_audio' in next));
 // An older search must not overwrite newer results, even if the transport ignores abort.
 await model.fill('slow/model');await page.waitForFunction(()=>window.queries.some(q=>q.query==='slow/model'));
 await model.fill('minimax/hailuo-3');await ready('MiniMax: H3');
 await page.waitForTimeout(750);assert.ok((await status.innerText()).includes('MiniMax: H3'));
 assert.equal(await page.evaluate(()=>window.queries.find(q=>q.query==='slow/model').signal.aborted),true);
 await page.getByRole('button',{name:'Reset video options'}).click();
 // Free path input survives missing catalog entries, switching conversations, and reload.
 await model.fill('new-provider/not-yet-listed');await ready('No matching video model');assert.equal(await generate.isDisabled(),true);
 await page.waitForTimeout(300);await page.evaluate(()=>window.navigate('two'));await provider.waitFor();
 await page.waitForFunction(()=>document.querySelector('.studio-video-provider select')?.value==='google');
 await page.evaluate(()=>window.navigate('one'));await ready('No matching video model');assert.equal(await model.inputValue(),'new-provider/not-yet-listed');
 await page.reload();await ready('No matching video model');assert.equal(await provider.inputValue(),'openrouter');assert.equal(await model.inputValue(),'new-provider/not-yet-listed');
 await page.evaluate(()=>window.offline=true);await model.fill('minimax/hailuo-3');await ready('catalog offline');assert.equal(await generate.isDisabled(),true);
 assert.equal(await model.inputValue(),'minimax/hailuo-3');await page.evaluate(()=>window.offline=false);await page.getByRole('button',{name:'Retry model search'}).click();await ready('MiniMax: H3');
 await page.evaluate(()=>window.orAvailable=false);await page.getByRole('button',{name:'Refresh studio'}).click();await ready('Configure OpenRouter');
 assert.equal(await provider.inputValue(),'openrouter');assert.equal(await model.inputValue(),'minimax/hailuo-3');assert.equal(await generate.isDisabled(),true);
 await page.evaluate(()=>window.orAvailable=true);await page.getByRole('button',{name:'Refresh studio'}).click();await ready('MiniMax: H3');
 // Reuse a Google provider draft without overwriting the custom path.
 await provider.selectOption('google');assert.equal(await duration.inputValue(),'6');await provider.selectOption('openrouter');await ready('MiniMax: H3');
 assert.equal(await model.inputValue(),'minimax/hailuo-3');
 await page.getByLabel('Generate audio').selectOption('true');
 await page.evaluate(()=>window.fixtureJobs=[{id:'legacy',kind:'video',status:'cancelled',operation:'text',
  input:{operation:'text',model:'omni',prompt:'Legacy scene',seconds:6,resolution:'1080p',aspect:'9:16',inputs:[]},
  provider_job:{provider:'openrouter',model:'minimax/hailuo-3',id:'upstream-cancel-123',status:'processing'}}]);
 await page.getByRole('button',{name:'Refresh studio'}).click();await ready('MiniMax: H3');
 assert.ok((await page.locator('.studio-provider-job').innerText()).includes('upstream-cancel-123'));
 assert.ok((await page.locator('.studio-provider-job').innerText()).includes('Local cancellation'));
 await page.getByText('Recent generations',{exact:false}).click();await page.getByRole('button',{name:'Reuse settings',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.studio-video-provider select')?.value==='google');
 await generate.click();await page.waitForFunction(()=>window.sent.length===1);
 const reused=await page.evaluate(()=>window.sent[0]);assert.equal(reused.provider,'google');assert.equal(reused.model,'omni');
 assert.equal(reused.seconds,6);assert.ok(!('generate_audio' in reused));assert.ok(!('seed' in reused));
 await provider.selectOption('openrouter');await ready('MiniMax: H3');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.locator('.generation-studio').evaluate(element=>{element.scrollTop=0;});
 await page.screenshot({path:process.env.STUDIO_SCREENSHOT,fullPage:true});assert.deepEqual(errors,[]);
 console.log('Provider isolation, exact payloads, source retention, search cancellation, reload and setup checks passed.');
}finally{await browser.close();}
"""
    run_js(source, timeout=75, env={**os.environ, 'STUDIO_WIDTH': str(viewport[0]),
                                  'STUDIO_HEIGHT': str(viewport[1]),
                                  'STUDIO_SCREENSHOT': str(tmp_path / 'video-studio.png')})
