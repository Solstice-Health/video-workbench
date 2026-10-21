import htm from './vendor/htm.mjs';
const {createElement, useState, useEffect, useRef} = window.React;
const html = htm.bind(createElement);
const {Button, Icon} = window.Eeva;

const emptyConfig = () => ({name: '', url: '', auth: 'auto', bearer_token: '', client_id: '', client_secret: '', scope: ''});
export function remoteMCPConfig(config) {
  const result = {name: config.name.trim(), url: config.url.trim(), auth: config.auth || 'auto'};
  if (result.auth === 'bearer' && config.bearer_token?.trim()) result.bearer_token = config.bearer_token.trim();
  if (result.auth === 'auto') {
    result.client_id = (config.client_id || '').trim();
    result.scope = (config.scope || '').trim();
    if (config.client_secret?.trim()) result.client_secret = config.client_secret.trim();
  }
  return result;
}

export function remoteToolAccess(tool) {
  return tool.annotations?.readOnlyHint === true && tool.annotations?.destructiveHint !== true
    ? 'Read only' : tool.annotations?.destructiveHint === true ? 'Can delete or replace data' : 'May change data';
}

export function remoteMCPGuidance(serverURL) {
  try {
    const url = new URL(serverURL);
    if (url.origin === 'https://mcp.figma.com' && !url.username && !url.password && /^\/mcp\/?$/.test(url.pathname)) {
      return {
        text: 'Figma’s hosted MCP requires approval for each client, including Workbench. Your Figma account sign-in comes after that approval. A regular Figma REST app ID does not grant MCP access.',
        href: 'https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/',
        label: 'Figma setup and client approval',
      };
    }
  } catch {}
  return null;
}

function ConnectionGuidance({url}) {
  const guidance = remoteMCPGuidance(url);
  return guidance && html`<p className="remote-mcp-note">${guidance.text} <a href=${guidance.href} target="_blank" rel="noopener noreferrer">${guidance.label}</a>.</p>`;
}

function ConnectionFields({value, onChange, disabled, saved = false}) {
  const update = key => event => onChange(previous => ({...previous, [key]: event.target.value}));
  return html`<div className="remote-mcp-fields">
    <label><span>Connection name</span><input required maxLength="80" autoComplete="off" value=${value.name} disabled=${disabled} placeholder="Design tools" onChange=${update('name')}/></label>
    <label><span>MCP server URL</span><input type="url" required autoComplete="off" spellCheck=${false} value=${value.url} disabled=${disabled} placeholder="https://example.com/mcp" onChange=${update('url')}/></label>
    ${!saved && html`<${ConnectionGuidance} url=${value.url}/>`}
    <details className="remote-mcp-advanced"><summary><${Icon} name="chevron-right" size=${12}/>Authentication settings</summary>
      <label><span>Authentication</span><select value=${value.auth} disabled=${disabled} onChange=${update('auth')}>
        <option value="auto">Automatic · public or browser sign-in</option><option value="none">Public · no authentication</option><option value="bearer">Bearer token</option>
      </select></label>
      ${value.auth === 'bearer' && html`<label><span>Bearer token</span><input type="password" autoComplete="new-password" required=${!saved} value=${value.bearer_token} disabled=${disabled} placeholder=${saved ? 'Leave blank to keep the saved token' : 'Paste the token supplied by your server'} onChange=${update('bearer_token')}/></label>`}
      ${value.auth === 'auto' && html`<div className="remote-mcp-oauth-fields">
        <p>Workbench opens the server’s sign-in page when required. Servers that accept Workbench’s automatic registration need no app setup. Otherwise, use credentials registered for Workbench with that server. Some providers require client approval first.</p>
        <label><span>Client ID <span className="caption">(optional)</span></span><input autoComplete="off" value=${value.client_id} disabled=${disabled} onChange=${update('client_id')}/></label>
        <label><span>Client secret <span className="caption">(optional)</span></span><input type="password" autoComplete="new-password" value=${value.client_secret} disabled=${disabled} placeholder=${saved ? 'Leave blank to keep the saved secret' : 'Only if required by the server'} onChange=${update('client_secret')}/></label>
        <label><span>Scopes <span className="caption">(optional)</span></span><input autoComplete="off" value=${value.scope} disabled=${disabled} placeholder="Use the server’s default scopes" onChange=${update('scope')}/></label>
        <label><span>Callback URL</span><input readOnly value=${`${location.origin}/api/integrations/mcp/callback`} onFocus=${event => event.target.select()}/></label>
      </div>`}
    </details>
  </div>`;
}

function RemoteConnection({item, busy, authPending, activeConnectionId, onConnect, onAction, stateLabel}) {
  const tools = item.tools || [];
  const signature = JSON.stringify(tools.map(tool => [tool.name, !!tool.enabled]));
  const [enabled, setEnabled] = useState(() => tools.filter(tool => tool.enabled).map(tool => tool.name));
  const [search, setSearch] = useState('');
  const [config, setConfig] = useState(() => ({...emptyConfig(), ...item, auth: item.auth === 'oauth' ? 'auto' : item.auth || 'auto', bearer_token: '', client_secret: ''}));
  useEffect(() => {setEnabled(tools.filter(tool => tool.enabled).map(tool => tool.name));}, [signature]);
  useEffect(() => {setConfig({...emptyConfig(), ...item, auth: item.auth === 'oauth' ? 'auto' : item.auth || 'auto', bearer_token: '', client_secret: ''});}, [item.name, item.url, item.auth, item.client_id, item.scope]);
  const changed = tools.some(tool => !!tool.enabled !== enabled.includes(tool.name));
  const visible = tools.filter(tool => `${tool.name} ${tool.description || ''}`.toLowerCase().includes(search.toLowerCase()));
  const toggle = name => setEnabled(previous => previous.includes(name) ? previous.filter(value => value !== name) : [...previous, name]);
  return html`<article className="remote-mcp-card">
    <div className="remote-mcp-card-heading"><h4>${item.name}</h4><span className=${`integration-state ${item.connected ? 'available' : ''}`}><${Icon} name=${item.connected ? 'check' : item.state === 'error' ? 'circle-alert' : 'info'} size=${12}/>${stateLabel(item)}</span></div>
    <p className="remote-mcp-address">${item.url}</p>
    ${item.message && html`<p className="remote-mcp-note">${item.message}</p>`}
    ${!item.connected && html`<${ConnectionGuidance} url=${item.url}/>`}
    <div className="remote-mcp-actions"><${Button} variant="secondary" size="sm" disabled=${busy || (authPending && (item.state !== 'connecting' || activeConnectionId === item.id))} onClick=${() => onConnect(item)}>${item.connected ? 'Reconnect' : item.state === 'connecting' ? 'Continue connection' : 'Connect'}<//>
      <${Button} variant="text" size="sm" disabled=${busy || !item.connected} onClick=${() => onAction(item.id, 'refresh')}>Refresh tools<//>
      <${Button} variant="text" size="sm" disabled=${busy || !['connected','connecting','error'].includes(item.state)} onClick=${() => onAction(item.id, 'disconnect')}>Disconnect<//>
      <${Button} variant="text" size="sm" disabled=${busy} onClick=${() => onAction(item.id, 'remove')}>Remove<//></div>
    ${tools.length > 0 && html`<details className="remote-mcp-tools"><summary><${Icon} name="chevron-right" size=${12}/>${tools.filter(tool => tool.enabled).length} of ${tools.length} tools enabled</summary>
      <p className="remote-mcp-note">Only enabled tools are available to the agent. Read tools are enabled initially; enable other tools when you want the agent to use them.</p>
      ${tools.length > 8 && html`<label className="remote-mcp-tool-search"><span>Find a tool</span><input type="search" value=${search} onChange=${event => setSearch(event.target.value)} placeholder="Search tools"/></label>`}
      <div className="remote-mcp-tool-list">${visible.map(tool => html`<label key=${tool.name} className="remote-mcp-tool">
        <input type="checkbox" checked=${enabled.includes(tool.name)} disabled=${busy} onChange=${() => toggle(tool.name)}/>
        <span><span className="remote-mcp-tool-name">${tool.name}</span><span className=${`remote-mcp-tool-access ${remoteToolAccess(tool) === 'Read only' ? '' : 'requires-choice'}`}>${remoteToolAccess(tool)}</span>${tool.description && html`<span className="remote-mcp-tool-description">${tool.description}</span>`}</span>
      </label>`)}</div>
      ${!visible.length && html`<p className="remote-mcp-note">No tools match this search.</p>`}
      <div className="remote-mcp-actions"><${Button} variant="secondary" size="sm" disabled=${busy || !changed} onClick=${() => onAction(item.id, 'configure', {enabled_tools: enabled})}>Save tool access<//>${changed && html`<span className="caption">Changes apply after saving</span>`}</div>
    </details>`}
    <details className="remote-mcp-settings"><summary><${Icon} name="chevron-right" size=${12}/>Connection settings</summary>
      <form className="remote-mcp-form" onSubmit=${async event => {event.preventDefault(); if (await onAction(item.id, 'configure', remoteMCPConfig(config))) setConfig(previous => ({...previous, bearer_token: '', client_secret: ''}));}}>
        <${ConnectionFields} value=${config} onChange=${setConfig} disabled=${busy} saved/>
        <${Button} variant="secondary" size="sm" type="submit" disabled=${busy}>Save connection settings<//>
      </form>
    </details>
  </article>`;
}

export function RemoteMCPConnections({items, busy, authPending, activeConnectionId, onConnect, onAdd, onAction, stateLabel}) {
  const [adding, setAdding] = useState(false), [config, setConfig] = useState(emptyConfig);
  const nameField = useRef(null), addButton = useRef(null), wasAdding = useRef(false);
  useEffect(() => {
    if (adding) nameField.current?.querySelector('input')?.focus();
    else if (wasAdding.current) addButton.current?.focus();
    wasAdding.current = adding;
  }, [adding]);
  const cancel = () => {setAdding(false); setConfig(emptyConfig());};
  return html`<section className="remote-mcp-section" aria-labelledby="remote-mcp-title">
    <div className="remote-mcp-heading"><div><h3 id="remote-mcp-title">Your MCP servers</h3><p>Connect directly to a server using its URL.</p></div>
      ${!adding && html`<${Button} ref=${addButton} variant="primary" size="sm" disabled=${busy || authPending} onClick=${() => setAdding(true)}><${Icon} name="plus" size=${14}/>Add MCP server<//>`}
    </div>
    ${adding && html`<form className="remote-mcp-form remote-mcp-add" ref=${nameField} onSubmit=${async event => {event.preventDefault(); if (await onAdd(remoteMCPConfig(config))) {setConfig(emptyConfig()); setAdding(false);}}}>
      <${ConnectionFields} value=${config} onChange=${setConfig} disabled=${busy}/>
      <p className="remote-mcp-note">Use a remote Streamable HTTP endpoint or a local server URL. Credentials stay on this computer.</p>
      <div className="remote-mcp-actions"><${Button} variant="primary" size="sm" type="submit" disabled=${busy || authPending}>Add and connect<//><${Button} variant="text" size="sm" disabled=${busy} onClick=${cancel}>Cancel<//></div>
    </form>`}
    <div className="remote-mcp-connections">${items.map(item => html`<${RemoteConnection} key=${item.id} item=${item} busy=${busy} authPending=${authPending} activeConnectionId=${activeConnectionId} onConnect=${onConnect} onAction=${onAction} stateLabel=${stateLabel}/>`)}
      ${!items.length && !adding && html`<div className="remote-mcp-empty"><${Icon} name="link" size=${18}/><p>Add a public server, sign in through your browser, or use a server token.</p></div>`}
    </div>
  </section>`;
}
