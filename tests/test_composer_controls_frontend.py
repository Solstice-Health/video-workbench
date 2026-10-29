"""Composer choices and real App message callbacks, without browser automation."""

import pytest
from test_steering_frontend import run_js


def run_composer_js(source):
    run_js(r"""
const {ComposerControls,composerPreference,composerMessage,rendererInstruction,
 readComposerPreferences,saveComposerPreferences,rendererOptions,MODE_HELP}=
 await import('../../src/vwb/web/static/composer-controls.js');
const preferences=new Map();
globalThis.localStorage={getItem:key=>preferences.get(key)??null,setItem:(key,value)=>preferences.set(key,value)};
const controls=()=>all(view).find(node=>node.type===ComposerControls);
const choosePreference=(mode,renderers)=>{controls().props.onChange({mode,renderers});render();};
""" + source)


def test_preference_storage_and_messages_are_explicit_and_auto_is_default():
    run_composer_js(r"""
assert.deepEqual(composerPreference(),{mode:'agentic',renderers:[]});
assert.deepEqual(composerPreference({mode:'bad',renderers:['browser','browser','unknown','remotion']}),
 {mode:'agentic',renderers:['remotion','browser']});
for(const mode of ['ask','storyboard','agentic']){
 assert.equal(composerMessage('  Make a clip  ',{mode,renderers:[]}),'Make a clip');
 const message=composerMessage('Make a clip',{mode,renderers:['gsap','browser']});
 assert.equal(message,'Make a clip\n\nCanvas renderer preference: allowed renderer IDs: browser, gsap. Choose one renderer per output from these options using canvas_new(renderer=...) or canvas_set_renderer; do not overlay multiple renderers.');
 assert.equal(composerMessage('',{mode,renderers:['browser']}),rendererInstruction({renderers:['browser']}));
}
assert.deepEqual(readComposerPreferences(),{});
saveComposerPreferences({'r-one':{mode:'ask',renderers:['browser']},'r-two':{mode:'storyboard',renderers:[]}});
assert.equal(readComposerPreferences()['r-one'].mode,'ask');
assert.deepEqual(readComposerPreferences()['r-two'].renderers,[]);
preferences.set('vwb-composer-preferences','invalid JSON');assert.deepEqual(readComposerPreferences(),{});
preferences.set('vwb-composer-preferences','[]');assert.deepEqual(readComposerPreferences(),{});
globalThis.localStorage={getItem(){throw Error('Unavailable');},setItem(){throw Error('Full');}};
assert.deepEqual(readComposerPreferences(),{});saveComposerPreferences({});
""")


@pytest.mark.parametrize("mode", ["queue", "now"])
def test_renderer_preferences_reach_saved_messages_without_affecting_answers(mode):
    run_composer_js(f"const delivery='{mode}';" + r"""
ticket.mode='blocking';ticket.waiters={live:{expires_at:Date.now()/1000+60}};
await mount();assert.equal(controls(),undefined);
purpose('New instruction');choosePreference('ask',['gsap','browser']);
composer().props.onValueChange('Make a title card');render();await attach();
purpose('Answer question');assert.equal(controls(),undefined);
composer().props.question.props.setAnswer('deny');render();
composer().props.onValueChange('Only the intro');render();
if(delivery==='now')await SteeringAction(steering().props).props.onClick();else await composer().props.onSend(composer().props.value);
await flush();render();
assert.deepEqual(submissions().at(-1).body,{ticket:ticket.id,option:'deny',text:'Only the intro',mode:delivery});
purpose('New instruction');
assert.deepEqual(controls().props.preference,{mode:'ask',renderers:['browser','gsap']});
assert.equal(composer().props.value,'Make a title card');
if(delivery==='now')await SteeringAction(steering().props).props.onClick();else await composer().props.onSend(composer().props.value);
await flush();render();
const sent=submissions().at(-1);
assert.equal(sent.body.message,composerMessage('Make a title card',{renderers:['browser','gsap']}));
assert.equal(sent.body.mode,delivery);assert.deepEqual(sent.body.attachments,['upload-test']);
assert.ok(!('composer_mode' in sent.body));assert.ok(!('renderers' in sent.body));
assert.equal(composer().props.value,'');assert.ok(!words(composer().props.attachments).includes('notes.txt'));
// Preferences persist for the next instruction, rather than changing already queued text.
choosePreference('storyboard',['canvas2d']);
assert.equal(sent.body.message,composerMessage('Make a title card',{renderers:['browser','gsap']}));
assert.equal(readComposerPreferences()['r-test'].mode,'storyboard');
""")


def test_preferences_follow_conversation_switches_reload_and_stay_out_of_qc():
    run_composer_js(r"""
snapshot.asks=[];
saveComposerPreferences({'r-test':{mode:'ask',renderers:['browser']},'r-other':{mode:'storyboard',renderers:['canvas2d']}});
await mount();assert.deepEqual(controls().props.preference,{mode:'ask',renderers:['browser']});
const navigation=()=>all(view).find(node=>node.type?.name==='ProjectNavigation');
navigation().props.onChoose('r-other');await flush();render();
assert.deepEqual(controls().props.preference,{mode:'storyboard',renderers:['canvas2d']});
choosePreference('agentic',['remotion']);
navigation().props.onChoose('r-test');await flush();render();
assert.deepEqual(controls().props.preference,{mode:'ask',renderers:['browser']});
all(view).find(node=>node.type==='Tabs').props.onValueChange('qc');render();
assert.equal(controls(),undefined);
const qc=all(view).find(node=>node.type?.name==='QCPanel');
assert.equal(qc.props.visible,true);
assert.ok(!('preference' in qc.props));assert.ok(!('composerMessage' in qc.props));
all(view).find(node=>node.type==='Tabs').props.onValueChange('edit');render();
assert.deepEqual(controls().props.preference,{mode:'ask',renderers:['browser']});
// A fresh App mount restores choices from storage.
hooks=[];effects=[];await mount();
assert.deepEqual(controls().props.preference,{mode:'ask',renderers:['browser']});
assert.deepEqual(readComposerPreferences()['r-other'],{mode:'agentic',renderers:['remotion']});
""")


@pytest.mark.parametrize("create_by_import", [True, False])
def test_new_conversation_and_import_workspace_keep_selected_preferences(create_by_import):
    run_composer_js(f"const createByImport={str(create_by_import).lower()};" + r"""
snapshot.asks=[];snapshot.active=null;
const originalFetch=fetch;
globalThis.fetch=async(url,options={})=>{
 if(url==='/api/requests'&&options.method==='POST'){
  calls.push({url,options,body:JSON.parse(options.body)});
  return {ok:true,json:async()=>({request_id:'r-created'})};
 }
 if(url==='/api/workspaces')return {ok:true,json:async()=>({request_id:'r-imported'})};
 return originalFetch(url,options);
};
await mount();all(view).find(node=>node.type?.name==='ProjectNavigation').props.onChoose(null);render();
assert.deepEqual(controls().props.preference,{mode:'agentic',renderers:[]});
choosePreference('storyboard',['canvas2d']);
await attach(); // Attachment-only send must still carry the instruction.
if(createByImport){
 const dialog=all(view).find(node=>node.type?.name==='LocalImportDialog');
 assert.equal(await dialog.props.ensureWorkspace(),'r-imported');await flush();render();
 assert.deepEqual(controls().props.preference,{mode:'storyboard',renderers:['canvas2d']});
}
assert.equal(composer().props.canSend,true);
await composer().props.onSend('');await flush();render();
const sent=calls.filter(call=>call.options.method==='POST'&&(call.url==='/api/requests'||call.url.endsWith('/messages'))).at(-1);
assert.equal(sent.body.message,rendererInstruction({renderers:['canvas2d']}));
assert.deepEqual(sent.body.attachments,['upload-test']);
assert.deepEqual(controls().props.preference,{mode:'storyboard',renderers:['canvas2d']});
const id=createByImport?'r-imported':'r-created';
assert.deepEqual(readComposerPreferences()[id],{mode:'storyboard',renderers:['canvas2d']});
assert.equal(readComposerPreferences().new,undefined);
all(view).find(node=>node.type?.name==='ProjectNavigation').props.onChoose(null);render();
assert.deepEqual(controls().props.preference,{mode:'agentic',renderers:[]});
""")


@pytest.mark.parametrize("catalog_available", [True, False])
def test_picker_native_controls_catalog_fallback_and_explanations(catalog_available):
    run_composer_js(f"const available={str(catalog_available).lower()};" + r"""
const catalog={renderers:[{id:'browser',label:'Updated browser label',description:'Browser canvas description',pin:'test'},
 {id:'gsap',label:'Updated GSAP label'}],default:'remotion'};
assert.deepEqual(rendererOptions(catalog).map(row=>row.id),['remotion','browser','gsap','canvas2d']);
let props={preference:composerPreference(),api:async url=>{
 assert.equal(url,'/api/canvas-renderers');if(!available)throw Error('Offline');return catalog;
},onChange:value=>props={...props,preference:composerPreference(value)}};
function paint(){cursor=0;view=ComposerControls(props);const pending=effects;effects=[];pending.forEach(run=>run());}
const select=()=>all(view).find(node=>node.type==='select');
const boxes=()=>all(view).filter(node=>node.type==='input');
paint();await flush();paint();
assert.equal(boxes().length,5);assert.equal(boxes()[0].props.checked,true);
assert.equal(all(view).find(node=>node.type==='label').props.title,MODE_HELP);
assert.ok(words(view).includes('UI preview only'));assert.ok(words(view).includes('do not alter execution or tool actions yet'));
assert.ok(words(view).includes('one renderer per output, not a simultaneous overlay'));
assert.equal(words(all(view).find(node=>node.props.className==='composer-catalog-note')),available?
 'Your choices are included with your message.':'Catalog unavailable. Showing built-in options; runtime availability is not verified.');
assert.deepEqual(all(view).filter(node=>node.type==='small').map(words),[
 'No added renderer preference','Video, text and shapes','HTML text / SVG','HTML / SVG motion','Shapes only; no text or captions']);
assert.ok(words(view).includes(available?'Updated browser label':'React / HTML + SVG'));
if(!available)assert.ok(words(view).includes('Catalog unavailable'));
assert.equal(select().props['aria-describedby'],'composer-mode-help');
assert.equal(all(view).find(node=>node.type==='fieldset').props['aria-describedby'],'composer-renderer-help');
select().props.onChange({target:{value:'ask'}});paint();assert.equal(select().props.value,'ask');
boxes()[2].props.onChange();paint();boxes()[3].props.onChange();paint();
assert.deepEqual(props.preference,{mode:'ask',renderers:['browser','gsap']});
assert.equal(boxes()[0].props.checked,false);assert.ok(words(view).includes('Canvas · 2 allowed'));
assert.ok(words(view).includes(rendererInstruction(props.preference)));
boxes()[2].props.onChange();paint();boxes()[3].props.onChange();paint();
assert.equal(boxes()[0].props.checked,true);assert.ok(!words(view).includes('Renderer instruction added'));
boxes()[1].props.onChange();paint();boxes()[0].props.onChange();paint();
assert.deepEqual(props.preference.renderers,[]);
props={...props,disabled:true};paint();assert.equal(select().props.disabled,true);
assert.equal(all(view).find(node=>node.type==='fieldset').props.disabled,true);
let focused=false,prevented=false,stopped=false;
const details=all(view).find(node=>node.props.className==='composer-renderers');
const target={open:true,querySelector:()=>({focus(){focused=true;}})};
details.props.onKeyDown({key:'Escape',currentTarget:target,preventDefault(){prevented=true;},stopPropagation(){stopped=true;}});
assert.equal(target.open,false);assert.equal(focused&&prevented&&stopped,true);
""")
