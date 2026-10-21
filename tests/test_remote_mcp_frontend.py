"""Authentication form payloads must not carry stale secrets between modes."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is optional for frontend checks")


def run_js(source):
    setup = """
import assert from 'node:assert/strict';
globalThis.window={React:{createElement(){}},Eeva:{}};
const {remoteMCPConfig,remoteToolAccess,remoteMCPGuidance}=await import('../../src/vwb/web/static/remote-mcp-control.js');
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", setup + source],
        cwd=ROOT / "generators/remotion", capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_authentication_switch_drops_unrelated_secrets_and_private_state():
    run_js("""
const form={name:' Example ',url:' https://example.com/mcp ',auth:'auto',
  bearer_token:' old-token ',client_id:' public-id ',client_secret:' private-secret ',
  scope:' tools:read ',tokens:{access_token:'must-not-submit'},tools:[{name:'private'}]};
assert.deepEqual(remoteMCPConfig(form),{
  name:'Example',url:'https://example.com/mcp',auth:'auto',
  client_id:'public-id',client_secret:'private-secret',scope:'tools:read'});
assert.deepEqual(remoteMCPConfig({...form,auth:'none'}),{
  name:'Example',url:'https://example.com/mcp',auth:'none'});
assert.deepEqual(remoteMCPConfig({...form,auth:'bearer'}),{
  name:'Example',url:'https://example.com/mcp',auth:'bearer',bearer_token:'old-token'});
const keepSaved=remoteMCPConfig({...form,client_secret:'   ',client_id:'',scope:''});
assert.ok(!Object.hasOwn(keepSaved,'client_secret'));
assert.equal(keepSaved.client_id,'');
assert.equal(keepSaved.scope,'');
assert.ok(!Object.hasOwn(remoteMCPConfig({...form,auth:'bearer',bearer_token:''}),'bearer_token'));
""")


def test_unknown_and_destructive_tools_are_never_presented_as_read_only():
    run_js("""
assert.equal(remoteToolAccess({}), 'May change data');
assert.equal(remoteToolAccess({annotations:{readOnlyHint:false}}),'May change data');
assert.equal(remoteToolAccess({annotations:{readOnlyHint:true}}),'Read only');
assert.equal(remoteToolAccess({annotations:{readOnlyHint:true,destructiveHint:true}}),'Can delete or replace data');
assert.equal(remoteToolAccess({annotations:{destructiveHint:true}}),'Can delete or replace data');
""")


def test_figma_setup_guidance_is_bound_to_the_official_endpoint():
    run_js("""
const help=remoteMCPGuidance('https://mcp.figma.com/mcp');
assert.ok(help.text.includes('approval'));
assert.ok(help.text.includes('REST app ID does not grant MCP access'));
assert.equal(help.href,'https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/');
assert.deepEqual(remoteMCPGuidance('https://mcp.figma.com/mcp/'),help);
for(const url of ['https://example.com/mcp','http://mcp.figma.com/mcp',
 'https://mcp.figma.com.example.com/mcp','https://mcp.figma.com@other.example/mcp',
 'https://user@mcp.figma.com/mcp','https://mcp.figma.com:8443/mcp',
 'https://mcp.figma.com/other','not a URL','']) {
 assert.equal(remoteMCPGuidance(url),null,url);
}
""")


def test_slow_oauth_discovery_keeps_popup_and_resumes_the_same_connection():
    run_js("""
let cursor=0, effects=[], hooks=[], setters=[], intervals=[];
const child={closed:false,location:{href:'about:blank'},close(){this.closed=true;}};
window.open=()=>child;
window.React={
 createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),
 useState(initial){const at=cursor++; if(!(at in hooks))hooks[at]=typeof initial==='function'?initial():initial;
  const set=value=>{hooks[at]=typeof value==='function'?value(hooks[at]):value;};setters[at]=set;return [hooks[at],set];},
 useRef(initial){const at=cursor++;return hooks[at]??=( {current:initial} );},
 useEffect(callback,deps){effects.push({callback,deps});}
};
globalThis.ReactDOM={createPortal:node=>node};
globalThis.document={body:{}};
globalThis.location={origin:'http://127.0.0.1:8765'};
globalThis.setInterval=(callback,ms)=>{intervals.push({callback,ms});return intervals.length;};
globalThis.clearInterval=()=>{};
const {IntegrationsControl}=await import('../../src/vwb/web/static/integrations-control.js');
const id='mcp_123456789abc', authorization='https://identity.example/authorize?state=opaque';
let connected=false, connectCalls=0;
const api=async (url,options)=>{
 if(url==='/api/integrations')return {items:[
  {id,generic:true,auth:'auto',state:connected?'connected':'connecting',name:'Example',tools:[]},
  {id:'figma_direct',available:true,state:'connected',connected:true,name:'Figma direct'}],
  config:{client_id:'registered',gateway_url:'https://gateway.example/mcp'}};
 assert.equal(url,`/api/integrations/mcp/${id}/connect`);
 assert.equal(options.method,'POST');
 connectCalls++;
 return connectCalls===1?{id,state:'connecting'}:{id,state:'connecting',authorization_url:authorization};
};
const render=()=>{cursor=0;effects=[];return IntegrationsControl({api,selected:[],onToggle(){},disabled:false});};
const all=node=>node&&typeof node==='object'?[node,...(node.children||[]).flatMap(all)]:[];
render();setters[1](true);
let view=render();
const panel=all(view).find(node=>node.type?.name==='RemoteMCPConnections');
await panel.props.onConnect({id,auth:'auto'});
assert.equal(connectCalls,1);
assert.equal(child.closed,false,'slow discovery must keep its sign-in popup');
assert.equal(child.location.href,'about:blank');
view=render();
const solstice=all(view).find(node=>node.children?.includes('Connect Solstice'));
assert.equal(solstice.props.disabled,true,'a pending direct login must not be replaced by another login');
effects.find(effect=>effect.deps?.[1]===id).callback();
const poll=intervals.find(interval=>interval.ms===1500);
await poll.callback();
assert.equal(connectCalls,2,'discovery resumes the existing connection');
assert.equal(child.location.href,authorization);
assert.equal(child.closed,false);
await poll.callback();
assert.equal(connectCalls,2,'after discovering the URL, polling only reads status');
connected=true;
await poll.callback();
assert.equal(child.closed,true,'successful authorization closes its own popup');
""")
