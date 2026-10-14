"""Pure pixel geometry and request contracts for the responsive studios."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vwb.gen.images import valid_size

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Node is optional for frontend checks')


def run_js(source):
    result = subprocess.run([NODE, '--input-type=module', '-e', source],
                            cwd=ROOT / 'generators/remotion', capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_preview_coordinates_placement_and_region_are_pixel_exact():
    run_js("""
import assert from 'node:assert/strict';
import {pointInCanvas,fitPlacement,scalePlacement,movePlacement,boxFromPoints} from '../../src/vwb/web/static/generation-geometry.js';
assert.deepEqual(pointInCanvas(250,150,{left:50,top:50,width:400,height:200},1600,800),[800,400]);
assert.deepEqual(pointInCanvas(-1,999,{left:0,top:0,width:100,height:100},1000,500),[0,500]);
const p=fitPlacement({width:2000,height:1000},[1024,1024]);
assert.deepEqual(p,[103,307,819,410]);
assert.deepEqual(movePlacement(p,-999,999,[1024,1024]),[0,614,819,410]);
for(const scale of [.1,.9,1.1,10]) {
 const next=scalePlacement(p,scale,[1024,1024]);
 assert.ok(next[0]>=0 && next[1]>=0 && next[0]+next[2]<=1024 && next[1]+next[3]<=1024);
 assert.ok(Math.abs(next[2]/next[3]-p[2]/p[3])<.04);
}
assert.deepEqual(boxFromPoints([900.5,700.5],[200.2,100.2],[1024,800]),[200,100,701,601]);
""")


def test_effective_image_size_matches_adapter_normalization():
    sizes = [(1024, 1024), (1000, 1000), (800, 1200), (1080, 1920), (1600, 900),
             (3840, 3840), (480, 160), (800, 808), (824, 824), (2400, 800)]
    expected = [[list(size), list(valid_size(*size))] for size in sizes]
    run_js(f"""
import assert from 'node:assert/strict';
import {{imageSize}} from '../../src/vwb/web/static/generation-geometry.js';
for(const [input,expected] of {json.dumps(expected)}) assert.deepEqual(imageSize(input),expected);
for(const value of [[0,100],['',100],['oops',100],[100,3841],[16,1024],[48,16]]) assert.equal(imageSize(value),null);
""")


def test_mode_contracts_preserve_provenance_and_reject_missing_masks():
    run_js("""
import assert from 'node:assert/strict';
import {draftDefaults,requestBody,inputLimit,inputRef} from '../../src/vwb/web/static/generation-geometry.js';
const id='0123456789abcdef', source={path:'work/studio/a.png',upload_id:id,width:800,height:800};
assert.deepEqual(inputRef(source),{upload_id:id});
assert.deepEqual(inputRef({ref:'slot:test@1',path:'assets/a.png'}),{ref:'slot:test@1'});
let d={...draftDefaults('image'),operation:'inpaint',prompt:'Replace only the mask',sources:[source]};
assert.throws(()=>requestBody(d,'image',id),/Paint/);
const p=requestBody(d,'image',id,{path:'work/mask.png'});
assert.deepEqual(p.inputs,[{upload_id:id}]); assert.deepEqual(p.mask,{path:'work/mask.png'}); assert.ok(!('place' in p));
d={...d,operation:'outpaint',place:[40,40,800,800]};
assert.deepEqual(requestBody(d,'image',id).place,[40,40,800,800]);
d={...d,operation:'remove_background',subject:'Bottle',prompt:'',box:[0,0,500,600]};
const extraction=requestBody(d,'image',id); assert.equal(extraction.subject,'Bottle'); assert.deepEqual(extraction.box,[0,0,500,600]);
assert.equal(extraction.background_method,'matte');
assert.equal(requestBody({...d,subject:''},'image',id).subject,'');
assert.throws(()=>requestBody({...d,background_method:'sunburst',subject:''},'image',id),/subject/);
assert.ok(!('mask' in extraction)); assert.ok(!('place' in extraction));
assert.throws(()=>requestBody({...d,sources:[source,source]},'image',id),/extra/);
const video={...draftDefaults('video'),prompt:'Slow orbit',model:'offered-model'};
assert.equal(requestBody(video,'video',id).operation,'text');
assert.throws(()=>requestBody({...video,operation:'frames'},'video',id),/source/);
assert.equal(inputLimit('video','frames'),2); assert.equal(inputLimit('video','reference'),3);
assert.equal(inputLimit('video','edit'),1);
""")


def test_workspace_drag_scope_and_persisted_drafts_exclude_urls():
    run_js("""
import assert from 'node:assert/strict';
import {workspaceDrop,safeDraft,draftDefaults,isActiveJob} from '../../src/vwb/web/static/generation-geometry.js';
assert.equal(workspaceDrop(JSON.stringify({requestId:'r1',path:'work/image.png'}),'r1').path,'work/image.png');
assert.throws(()=>workspaceDrop(JSON.stringify({requestId:'r2',path:'work/image.png'}),'r1'),/conversation/);
for(const path of ['../secret','/etc/passwd','work/../secret']) assert.throws(()=>workspaceDrop(JSON.stringify({requestId:'r1',path}),'r1'));
const d={...draftDefaults('image'),sources:[{path:'work/a.png',url:'/preview/a?credential=private',upload_id:'id'}],strokes:[{width:12,points:[[1,2]]}]};
const saved=safeDraft(d); assert.equal(saved.sources[0].path,'work/a.png'); assert.ok(!('url' in saved.sources[0]));
assert.equal(d.sources[0].url,'/preview/a?credential=private'); assert.deepEqual(saved.strokes,d.strokes);
assert.equal(isActiveJob({status:'running'}),true); assert.equal(isActiveJob({status:'interrupted'}),false);
globalThis.window={React:{createElement(){}},Eeva:{}};
const {GenerationStudio}=await import('../../src/vwb/web/static/generation-studio.js');
assert.equal(typeof GenerationStudio,'function'); // Import must not need DOM, storage, or hooks.
""")


def test_mask_draw_and_erase_use_alpha_not_black_opaque_pixels():
    run_js("""
import assert from 'node:assert/strict';
import {paintStrokes} from '../../src/vwb/web/static/generation-geometry.js';
const events=[];
const ctx={beginPath(){},arc(){},moveTo(){},lineTo(){},fill(){events.push([this.globalCompositeOperation,this.fillStyle,this.lineWidth]);},stroke(){events.push([this.globalCompositeOperation,this.strokeStyle,this.lineWidth]);}};
paintStrokes(ctx,[{points:[[2,3]],width:20},{points:[[2,3],[5,6]],width:10,erase:true}]);
assert.deepEqual(events,[['source-over','#ffffff',20],['destination-out','#ffffff',10]]);
assert.equal(ctx.globalCompositeOperation,'source-over');
""")


def test_studio_renders_capability_modes_results_and_reduced_surface_without_dom():
    run_js("""
import assert from 'node:assert/strict';
let state=[],index=0,nodes=[];
globalThis.window={React:{
 createElement(type,props,...children){return {type,props:{...props,children}};},
 useState(initial){const at=index;const value=index<state.length?state[index]:typeof initial==='function'?initial():initial;index++;return [value,v=>{state[at]=typeof v==='function'?v(state[at]):v;}];},
 useRef(current){return {current};},useEffect(){}
},Eeva:{Button:'button',Icon:'i',Tooltip:'span',MessageBar:'aside',SolsticeMark:'svg'}};
const {GenerationStudio}=await import('../../src/vwb/web/static/generation-studio.js');
const {draftDefaults}=await import('../../src/vwb/web/static/generation-geometry.js');
const capabilities={image:{available:true,models:[{id:'sunburst'}],operations:['create','edit','inpaint','outpaint','remove_background'],quality:['high'],background_methods:[{id:'matte',label:'Preserve source (local)',available:true},{id:'sunburst',label:'Regenerate cutout (Sunburst)',available:true}]},
 video:{available:true,models:[{id:'omni'}],operations:['text','frames','reference','edit','extend'],resolutions:['1080p'],aspects:['9:16'],seconds:{min:3,max:10}}};
const source={path:'work/a.png',url:'/preview/a.png',kind:'image',width:1024,height:768};
function walk(node,text=[]){
 if(node==null||typeof node==='boolean')return text;
 if(Array.isArray(node)){for(const x of node)walk(x,text);return text;}
 if(typeof node!=='object'){text.push(String(node));return text;}
 if(typeof node.type==='function')return walk(node.type(node.props),text);
 nodes.push(node);
 walk(node.props.children,text);return text;
}
for(const kind of ['image','video'])for(const operation of capabilities[kind].operations){
 const d={...draftDefaults(kind),operation,model:kind==='image'?'sunburst':'omni',sources:operation==='text'?[]:[source],markup:true};
 state=[d,capabilities,[],null,'',false,false,false,'',false,0];index=0;
 const text=walk(GenerationStudio({kind,requestId:'r1',api:()=>{throw Error('Rendering must not send a request');}})).join(' ');
 assert.ok(text.includes(operation==='remove_background'?'No provider charges.':'Provider charges apply.')); assert.ok(text.includes(kind==='image'?'Make room':'Put an idea'));
}
const job={id:'j1',kind:'image',operation:'create',status:'completed',created:'2026-10-14T00:00:00Z',input:{model:'sunburst',quality:'high'},
 result:{model:'sunburst',artifacts:[{path:'work/studio/images/j1/output.png',url:'/preview/result',mime:'image/png',width:1024,height:1024}],receipt_path:'work/studio/images/j1/receipt.json'}};
state=[{...draftDefaults('image'),showResult:true},capabilities,[job],job,'',false,false,false,'',false,0];index=0;
const result=walk(GenerationStudio({kind:'image',requestId:'r1',api(){}})).join(' ');
assert.ok(result.includes('Generation details'));assert.ok(result.includes('Edit this'));assert.ok(result.includes('Reuse settings'));
const localCaps={...capabilities,image:{...capabilities.image,available:false,models:[]}};
state=[{...draftDefaults('image'),operation:'remove_background',sources:[source]},localCaps,[],null,'',false,false,false,'',false,0];index=0;nodes=[];
const local=walk(GenerationStudio({kind:'image',requestId:'local',api(){}})).join(' ');
assert.ok(local.includes('Local matte'));assert.ok(!local.includes('Subject to keep'));assert.ok(!local.includes('Effort'));
const submit=nodes.find(n=>n.type==='button'&&n.props.type==='submit');assert.equal(submit.props.disabled,false);
const cutout={...job,operation:'remove_background',input:{background_method:'matte'},reusable_inputs:[{...source,ref:'slot:original@1'}],
 result:{...job.result,background_mask:{path:'work/background.png',url:'/preview/mask.png',ref:'slot:background@1',width:1024,height:768}}};
state=[{...draftDefaults('image'),showResult:true},capabilities,[cutout],cutout,'',false,false,false,'',false,0];index=0;nodes=[];
globalThis.Image=class {naturalWidth=1024;naturalHeight=768;set src(value){queueMicrotask(()=>this.onload());}};
const api=async url=>({path:url.includes('background')?'work/background.png':'work/a.png',url:'/preview/image.png',mime:'image/png'});
walk(GenerationStudio({kind:'image',requestId:'mask-flow',api}));
const action=nodes.find(n=>n.type==='button'&&n.props.children.flat(Infinity).includes('Use background mask'));
assert.ok(action);await action.props.onClick();
assert.equal(state[0].operation,'inpaint');assert.equal(state[0].sources[0].ref,'slot:original@1');
assert.equal(state[0].savedMask.ref,'slot:background@1');assert.equal(state[0].savedMask.width,1024);
assert.equal(state[0].showResult,false);assert.deepEqual(state[0].strokes,[]);
""")
