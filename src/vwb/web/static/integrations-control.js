import htm from './vendor/htm.mjs';
import {RemoteMCPConnections} from './remote-mcp-control.js';
const {createElement, useState, useEffect, useRef} = window.React;
const html = htm.bind(createElement);
const {Button, Icon, Tooltip, MessageBar} = window.Eeva;
const labels = {unconfigured: 'Needs setup', disconnected: 'Not connected', connecting: 'Connecting', connected: 'Connected', unavailable: 'Unavailable', error: 'Connection error'};
export function integrationState(item) {return labels[item.state] || 'Not reported';}
export function publicIntegrationConfig(config) {
  return {gateway_url: config.gateway_url.trim(), client_id: config.client_id.trim(), redirect_uri: config.redirect_uri.trim(),
    read_tools: Array.isArray(config.read_tools) ? config.read_tools : config.read_tools.split(/[\n,]/).map(s => s.trim()).filter(Boolean)};
}
export function IntegrationsControl({api, selected, onToggle, disabled}) {
  const [open, setOpen] = useState(false), [manage, setManage] = useState(false), [catalog, setCatalog] = useState(null);
  const [directConfig, setDirectConfig] = useState({client_id: '', client_secret: '', redirect_uri: ''});
  const [error, setError] = useState(''), [busy, setBusy] = useState(''), [position, setPosition] = useState({});
  const [authLink, setAuthLink] = useState(null);
  const [config, setConfig] = useState({gateway_url: '', client_id: '', redirect_uri: '', read_tools: ''});
  const popupTimer = useRef(null), popupStarted = useRef(0), popupConnection = useRef(null), discovering = useRef(false);
  const shell = useRef(null), trigger = useRef(null), manager = useRef(null), popup = useRef(null);
  const refresh = async () => {const next = await api('/api/integrations'); setCatalog(next); return next;};
  useEffect(() => {let live = true; api('/api/integrations').then(next => {if (live) setCatalog(next);}).catch(() => {}); return () => {live = false;};}, [api]);
  useEffect(() => {
    if (!open) return;
    const rect = trigger.current.getBoundingClientRect();
    setPosition({left: Math.max(8, Math.min(rect.left, window.innerWidth - 348)), bottom: window.innerHeight - rect.top + 8});
    refresh().catch(e => setError(e.message));
    const outside = e => {if (!shell.current?.contains(e.target) && !e.target.closest('.integration-popover')) setOpen(false);};
    const escape = e => {if (e.key === 'Escape') {e.stopPropagation(); setOpen(false); trigger.current?.focus();}};
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
    return () => {document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape);};
  }, [open]);
  useEffect(() => {
    const returned = event => {
      if (event.origin !== location.origin || event.source !== popup.current || event.data?.type !== 'vwb-integration-auth') return;
      popup.current = null; popupConnection.current = null; discovering.current = false; clearInterval(popupTimer.current); setBusy(''); setAuthLink(null);
      if (!event.data.ok) setError('The connection was not completed. You can try connecting again.');
      refresh().catch(e => setError(e.message));
    };
    window.addEventListener('message', returned); return () => window.removeEventListener('message', returned);
  }, [api]);
  const connecting = (catalog?.items || []).filter(item => item.generic && item.state === 'connecting').map(item => item.id).join(',');
  useEffect(() => {
    if (!connecting) return;
    let live = true, pending = false;
    const timer = setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        const next = await api('/api/integrations');
        if (!live) return;
        setCatalog(next);
        const current = (next.items || []).find(item => item.id === popupConnection.current);
        if (current && current.state !== 'connecting') {
          handleRemoteResult(current.id, current);
        } else if (current && discovering.current) {
          // Connect resumes the same host job and returns its OAuth URL once discovery finishes.
          const result = await api(`/api/integrations/mcp/${current.id}/connect`, {method: 'POST', body: '{}'});
          if (live && popupConnection.current === current.id) handleRemoteResult(current.id, result);
        }
      } catch (e) {if (live) setError(e.message);} finally {pending = false;}
    }, 1500);
    return () => {live = false; clearInterval(timer);};
  }, [api, connecting]);
  useEffect(() => () => clearInterval(popupTimer.current), []);
  useEffect(() => {
    if (!manage) return;
    const prior = document.activeElement;
    manager.current?.querySelector('button')?.focus();
    const key = e => {
      if (e.key === 'Escape') {e.preventDefault(); e.stopPropagation(); setManage(false);}
      if (e.key !== 'Tab') return;
      const nodes = [...manager.current.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),summary,a[href]')].filter(node => node.getClientRects().length);
      const first = nodes[0], last = nodes.at(-1);
      if (e.shiftKey && (document.activeElement === first || !manager.current.contains(document.activeElement))) {e.preventDefault(); last?.focus();}
      else if (!e.shiftKey && (document.activeElement === last || !manager.current.contains(document.activeElement))) {e.preventDefault(); first?.focus();}
    };
    document.addEventListener('keydown', key, true);
    return () => {document.removeEventListener('keydown', key, true); if (prior?.isConnected) prior.focus(); else trigger.current?.focus();};
  }, [manage]);
  const showManager = () => {
    const current = catalog?.config || {};
    setConfig({gateway_url: current.gateway_url || '', client_id: current.client_id || '', redirect_uri: current.redirect_uri || `${location.origin}${catalog?.setup?.callback_path || '/api/integrations/solstice/callback'}`, read_tools: (current.read_tools || []).join('\n')});
    const direct = catalog?.direct_figma;
    setDirectConfig({client_id: direct?.config?.client_id || '', client_secret: '', redirect_uri: direct?.config?.redirect_uri || `${location.origin}${direct?.setup?.callback_path || '/api/integrations/figma-direct/callback'}`});
    setOpen(false); setManage(true); setError('');
  };
  const mutate = async (action, body = {}) => {
    setBusy(action); setError('');
    try {await api(`/api/integrations/${action}`, {method: 'POST', body: JSON.stringify(body)}); await refresh(); return true;}
    catch (e) {setError(e.message); await refresh().catch(() => {}); return false;} finally {setBusy('');}
  };
  const connect = async (kind = 'solstice') => {
    if (connecting) {setError('Complete or disconnect the pending MCP sign-in before starting another connection.'); return;}
    const child = window.open('about:blank', 'vwb-integration-auth', 'popup,width=560,height=720');
    if (!child) {setError('Allow the sign-in popup, then choose Connect again.'); return;}
    popup.current = child; popupStarted.current = Date.now(); setBusy('connect'); setError('');
    clearInterval(popupTimer.current);
    popupTimer.current = setInterval(() => {
      if (popup.current !== child) {clearInterval(popupTimer.current); return;}
      if (child.closed || Date.now() - popupStarted.current > 300000) {
        clearInterval(popupTimer.current); popup.current = null; setBusy('');
        (kind === 'figma' ? api('/api/integrations/refresh', {method: 'POST', body: '{}'}).then(refresh) : kind === 'figma_direct' ? api('/api/integrations/figma-direct/refresh', {method: 'POST', body: '{}'}).then(refresh) : refresh()).catch(e => setError(e.message));
      }
    }, 1000);
    try {
      const result = await api(kind === 'figma_direct' ? '/api/integrations/figma-direct/connect' : kind === 'figma' ? '/api/integrations/figma/connect' : '/api/integrations/connect', {method: 'POST', body: '{}'});
      if (!result.authorization_url) throw new Error('The sign-in address was not returned. Try connecting again.');
      child.location.href = result.authorization_url;
    } catch (e) {child.close(); popup.current = null; clearInterval(popupTimer.current); setBusy(''); setError(e.message);}
  };
  const remoteAction = async (id, action, body = {}) => {
    setBusy(`mcp/${action}`); setError('');
    try {
      const result = await api(`/api/integrations/mcp/${id}/${action}`, {method: 'POST', body: JSON.stringify(body)});
      if (['remove','disconnect'].includes(action) && popupConnection.current === id) {
        popup.current?.close(); popup.current = null; popupConnection.current = null; discovering.current = false;
        clearInterval(popupTimer.current); setAuthLink(null);
      }
      await refresh(); return result || true;
    } catch (e) {setError(e.message); await refresh().catch(() => {}); return false;}
    finally {setBusy('');}
  };
  function handleRemoteResult(id, result) {
    if (popupConnection.current !== id) return;
    if (result.authorization_url) {
      discovering.current = false;
      const child = popup.current;
      if (child && !child.closed) child.location.href = result.authorization_url;
      else setAuthLink({id, url: result.authorization_url});
      clearInterval(popupTimer.current);
      popupTimer.current = setInterval(() => {
        if (popupConnection.current !== id) {clearInterval(popupTimer.current); return;}
        if (child?.closed) {
          popup.current = null; clearInterval(popupTimer.current);
          setAuthLink({id, url: result.authorization_url}); refresh().catch(e => setError(e.message));
        } else if (Date.now() - popupStarted.current > 300000) {
          clearInterval(popupTimer.current); refresh().catch(e => setError(e.message));
        }
      }, 1000);
    } else if (result.state === 'connecting') {
      discovering.current = true;
    } else {
      popup.current?.close(); popup.current = null; popupConnection.current = null; discovering.current = false;
      clearInterval(popupTimer.current); setAuthLink(null);
      if (result.state === 'error') setError(result.message || 'The server could not connect. Review its settings and try again.');
    }
  }
  const connectRemote = async (item, newConfig = null) => {
    // Open during the user gesture; public and token connections close this immediately.
    const child = ['auto','oauth'].includes(newConfig?.auth || item?.auth || 'auto')
      ? window.open('about:blank', 'vwb-integration-auth', 'popup,width=560,height=720') : null;
    let saved = item;
    popup.current = child; popupStarted.current = Date.now(); setAuthLink(null); setBusy('mcp/connect'); setError('');
    clearInterval(popupTimer.current);
    try {
      if (newConfig) {
        const result = await api('/api/integrations/mcp/add', {method: 'POST', body: JSON.stringify(newConfig)});
        saved = result.item;
      }
      popupConnection.current = saved.id;
      discovering.current = true;
      const result = await api(`/api/integrations/mcp/${saved.id}/connect`, {method: 'POST', body: '{}'});
      handleRemoteResult(saved.id, result);
      await refresh();
      return true;
    } catch (e) {
      child?.close(); popup.current = null; popupConnection.current = null; discovering.current = false;
      clearInterval(popupTimer.current); setError(e.message); await refresh().catch(() => {});
      // A saved connection remains editable even when its first connection fails.
      return !!saved && !!newConfig;
    } finally {setBusy('');}
  };
  const continueSignIn = () => {
    const child = window.open(authLink.url, 'vwb-integration-auth', 'popup,width=560,height=720');
    if (!child) {setError('Allow the sign-in popup, then choose Continue sign-in again.'); return;}
    popup.current = child; popupConnection.current = authLink.id; popupStarted.current = Date.now();
    handleRemoteResult(authLink.id, {authorization_url: authLink.url}); setAuthLink(null); setError('');
  };
  const items = catalog?.items || [], count = selected.length;
  const chosen = item => selected.some(r => r.id === item.id);
  return html`<div className="integration-control" ref=${shell}>
    <${Tooltip} content="Plugins and MCP connections"><button ref=${trigger} className="integration-trigger" aria-label="Plugins and MCP connections" aria-expanded=${open} disabled=${disabled} onClick=${() => setOpen(!open)}><${Icon} name="library" size=${14}/><span>${count ? `${count} selected` : 'Plugins'}</span><${Icon} name="chevron-down" size=${12}/></button><//>
    ${open && ReactDOM.createPortal(html`<section className="integration-popover" style=${position} aria-label="Plugins and MCP connections"><div className="integration-popover-heading"><h3>Plugins and MCP</h3><span className="caption">For your next message</span></div>
      ${error && html`<${MessageBar} tone="error">${error}<//>`}
      <div className="integration-options">${items.map(item => html`<button className="integration-option" key=${item.id} aria-pressed=${chosen(item)} onClick=${() => onToggle({id: item.id, kind: 'plugin', name: item.name, description: item.description})}>
        <span className="integration-checkbox"><${Icon} name=${chosen(item) ? 'check' : 'plus'} size=${13}/></span><span><strong>${item.name}<span>${item.kind === 'mcp' ? 'MCP' : 'Plugin'}</span></strong><span>${item.description}</span><span className=${`integration-state ${item.connected ? 'available' : ''}`}><${Icon} name=${item.connected ? 'check' : 'info'} size=${12}/>${integrationState(item)}${item.available && item.tools_count != null ? ` · ${item.tools_count} ${item.generic ? 'enabled tools' : 'read tools'}` : ''}</span></span></button>`)}
        ${!catalog && !error && html`<p className="navigator-empty">Reading connections…</p>`}${catalog && !items.length && html`<p className="navigator-empty">No integrations are configured.</p>`}</div>
      <p className="integration-help">Selections guide the agent. A disconnected integration still needs setup before its tools can run.</p><button className="integration-manage" onClick=${showManager}>Manage connections<${Icon} name="chevron-right" size=${14}/></button></section>`, document.body)}
    ${manage && ReactDOM.createPortal(html`<div className="integration-scrim" onPointerDown=${event => {if (event.target === event.currentTarget && !busy) setManage(false);}}><section className="integration-manager" role="dialog" aria-modal="true" aria-labelledby="integration-manager-title" ref=${manager}>
      <header><div><h2 id="integration-manager-title">Plugins and connections</h2><p>Connect the tools available to your workbench.</p></div><${Tooltip} content="Close connections"><${Button} variant="text" iconOnly aria-label="Close connections" onClick=${() => setManage(false)}><${Icon} name="x" size=${16}/><//><//></header>
      <div className="integration-manager-status" aria-live="polite">${error ? html`<${MessageBar} tone="error">${error}<//>` : busy || connecting ? html`<${MessageBar} tone="progress">${busy.endsWith('configure') ? 'Saving connection settings' : busy.endsWith('refresh') ? 'Checking available tools' : busy.endsWith('disconnect') ? 'Disconnecting' : busy.endsWith('remove') ? 'Removing connection' : busy === 'figma-mcp/connect' ? 'Connecting to Figma desktop' : busy === 'mcp/connect' ? 'Connecting to the MCP server' : connecting ? discovering.current ? 'Discovering server authentication' : 'Complete sign-in in your browser' : 'Opening sign-in'}…<//>` : html`<${MessageBar} tone="info">Connect any compatible remote MCP server directly. Solstice and Figma are also available below.<//>`}
        ${authLink && html`<div className="remote-mcp-sign-in"><span>Your server requires browser sign-in.</span><${Button} variant="secondary" size="sm" onClick=${continueSignIn}>Continue sign-in<//></div>`}</div>
      <div className="integration-manager-body"><${RemoteMCPConnections} items=${items.filter(item => item.generic)} busy=${!!busy} authPending=${!!connecting} activeConnectionId=${popupConnection.current} onConnect=${connectRemote} onAdd=${config => connectRemote(null, config)} onAction=${remoteAction} stateLabel=${integrationState}/>
      <h3 className="integration-providers-title">Built-in connections</h3><div className="integration-cards">${items.filter(item => !item.generic).map(item => html`<article key=${item.id}><div><h3>${item.name}</h3><span className=${`integration-state ${item.connected ? 'available' : ''}`}>${integrationState(item)}</span></div><p>${item.description}</p>${item.message && html`<p className="caption">${item.message}</p>`}${item.id === 'figma_direct' && item.account?.id && html`<p className="caption">${item.account.email || item.account.handle || item.account.id}</p>`}${item.tools_count != null && html`<span className="caption">${item.tools_count} available read tools</span>`}${item.id === 'figma_mcp' && html`<div className="integration-direct-actions"><${Button} variant="secondary" size="sm" disabled=${!!busy} onClick=${() => mutate('figma-mcp/connect')}>${item.connected ? 'Reconnect desktop MCP' : 'Connect desktop MCP'}<//><${Button} variant="text" size="sm" disabled=${!!busy || !item.connected} onClick=${() => mutate('figma-mcp/refresh')}>Check desktop MCP<//><${Button} variant="text" size="sm" disabled=${!!busy || !['connected','error'].includes(item.state)} onClick=${() => mutate('figma-mcp/disconnect')}>Disconnect desktop MCP<//></div>`}${item.id === 'figma_direct' && html`<div className="integration-direct-actions"><${Button} variant="secondary" size="sm" disabled=${!!busy || !!connecting || !item.available} onClick=${() => connect('figma_direct')}>${item.connected ? 'Reconnect Figma direct' : 'Connect Figma direct'}<//><${Button} variant="text" size="sm" disabled=${!!busy || !item.available} onClick=${() => mutate('figma-direct/refresh')}>Check Figma account<//><${Button} variant="text" size="sm" disabled=${!!busy || !['connected','connecting','error'].includes(item.state)} onClick=${() => mutate('figma-direct/disconnect')}>Disconnect Figma<//></div>`}${item.id === 'figma' && item.available && !item.connected && html`<${Button} variant="secondary" size="sm" disabled=${!!busy || !!connecting} onClick=${() => connect('figma')}>Connect Figma<//>`}</article>`)}</div>
      <div className="integration-connection-actions"><${Button} variant="secondary" size="sm" disabled=${!!busy || !!connecting || !catalog?.config?.client_id || !catalog?.config?.gateway_url} onClick=${() => connect('solstice')}>Connect Solstice<//><${Button} variant="text" size="sm" disabled=${!!busy} onClick=${() => mutate('refresh')}>Check tools<//><${Button} variant="text" size="sm" disabled=${!!busy || !catalog?.config?.client_id && !items.some(i => i.id === 'solstice' && ['connected','connecting','error'].includes(i.state))} onClick=${() => mutate('disconnect')}>Disconnect<//></div>
      <details className="integration-settings"><summary>Figma desktop MCP setup</summary>
        <p>Open your design in the Figma desktop app. Switch to Dev Mode with Shift+D. In the right sidebar, enable the desktop MCP server. Keep Figma open, then choose Connect desktop MCP above. You can send a frame link or ask the agent to use your current selection.</p>
        <p>This uses your signed-in desktop session. No app ID or secret is needed. Figma requires a Dev or Full seat on an eligible plan. <a href="https://developers.figma.com/docs/figma-mcp-server/local-server-installation/" target="_blank" rel="noopener noreferrer">Figma’s desktop setup guide</a>.</p>
        <p>The hosted MCP is a separate service and currently requires Figma approval for a new client such as Workbench.</p></details>
      <details className="integration-settings" open=${!catalog?.direct_figma?.config?.client_id}><summary>Figma direct settings</summary>
        <p>Create an app at <a href="https://www.figma.com/developers/apps" target="_blank" rel="noopener noreferrer">Figma’s apps page</a>. Add the callback below and enable file_content:read and current_user:read. You can test a draft as its owner; publish privately to allow your team to connect.</p>
        <form onSubmit=${async event => {event.preventDefault(); if (await mutate('figma-direct/configure', directConfig)) setDirectConfig(previous => ({...previous,client_secret:''}));}}>
          <label><span>Figma app Client ID</span><input required value=${directConfig.client_id} disabled=${!!busy} onChange=${e => setDirectConfig(previous => ({...previous,client_id:e.target.value}))}/></label>
          <label><span>Figma app Client Secret</span><input type="password" autoComplete="new-password" required=${!catalog?.direct_figma?.config?.client_secret_configured} placeholder=${catalog?.direct_figma?.config?.client_secret_configured ? 'Saved on this computer. Leave blank to keep it.' : 'Paste the secret from your Figma app'} value=${directConfig.client_secret} disabled=${!!busy} onChange=${e => setDirectConfig(previous => ({...previous,client_secret:e.target.value}))}/></label>
          <label><span>Figma direct callback URL</span><input type="url" required value=${directConfig.redirect_uri} disabled=${!!busy} onChange=${e => setDirectConfig(previous => ({...previous,redirect_uri:e.target.value}))}/></label>
          <p>Your app secret and sign-in stay on this computer, outside the agent’s workspace. Connect in your regular browser after saving.</p>
          <${Button} variant="secondary" size="sm" type="submit" disabled=${!!busy}>Save Figma settings<//>
        </form></details>
      <details className="integration-settings"><summary>Solstice connection settings</summary><p>These are public application settings. Your sign-in tokens stay on the host.</p>
        <form onSubmit=${event => {event.preventDefault(); mutate('configure', publicIntegrationConfig(config));}}>
          ${[['gateway_url','Gateway URL','url'],['client_id','Public application client ID','text'],['redirect_uri','Callback URL','url']].map(([key,label,type]) => html`<label key=${key}><span>${label}</span><input type=${type} required value=${config[key]} disabled=${!!busy} onChange=${e => setConfig(previous => ({...previous,[key]:e.target.value}))}/></label>`)}
          <label><span>Allowed read tools</span><textarea value=${config.read_tools} rows="3" placeholder="One tool name per line" disabled=${!!busy} onChange=${e => setConfig(previous => ({...previous,read_tools:e.target.value}))}/></label>
          <p className="caption">${catalog?.setup?.client_registration_required ? 'An OAuth application must be registered with the exact callback URL before connecting.' : 'Use the settings supplied by your workspace administrator.'}</p>
          <${Button} variant="secondary" size="sm" type="submit" disabled=${!!busy}>Save settings<//></form></details></div>
    </section></div>`, document.body)}
  </div>`;
}
