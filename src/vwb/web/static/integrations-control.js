import htm from './vendor/htm.mjs';
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
  const [error, setError] = useState(''), [busy, setBusy] = useState(''), [position, setPosition] = useState({});
  const [config, setConfig] = useState({gateway_url: '', client_id: '', redirect_uri: '', read_tools: ''});
  const popupTimer = useRef(null), popupStarted = useRef(0);
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
      popup.current = null; clearInterval(popupTimer.current); setBusy('');
      if (!event.data.ok) setError('The connection was not completed. You can try connecting again.');
      refresh().catch(e => setError(e.message));
    };
    window.addEventListener('message', returned); return () => window.removeEventListener('message', returned);
  }, [api]);
  useEffect(() => () => clearInterval(popupTimer.current), []);
  useEffect(() => {
    if (!manage) return;
    const prior = document.activeElement;
    manager.current?.querySelector('button')?.focus();
    const key = e => {
      if (e.key === 'Escape') {e.preventDefault(); e.stopPropagation(); setManage(false);}
      if (e.key !== 'Tab') return;
      const nodes = [...manager.current.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),a[href]')];
      const first = nodes[0], last = nodes.at(-1);
      if (e.shiftKey && (document.activeElement === first || !manager.current.contains(document.activeElement))) {e.preventDefault(); last?.focus();}
      else if (!e.shiftKey && (document.activeElement === last || !manager.current.contains(document.activeElement))) {e.preventDefault(); first?.focus();}
    };
    document.addEventListener('keydown', key, true);
    return () => {document.removeEventListener('keydown', key, true); prior?.isConnected && prior.focus();};
  }, [manage]);
  const showManager = () => {
    const current = catalog?.config || {};
    setConfig({gateway_url: current.gateway_url || '', client_id: current.client_id || '', redirect_uri: current.redirect_uri || `${location.origin}${catalog?.setup?.callback_path || '/api/integrations/solstice/callback'}`, read_tools: (current.read_tools || []).join('\n')});
    setOpen(false); setManage(true); setError('');
  };
  const mutate = async (action, body = {}) => {
    setBusy(action); setError('');
    try {await api(`/api/integrations/${action}`, {method: 'POST', body: JSON.stringify(body)}); await refresh(); return true;}
    catch (e) {setError(e.message); return false;} finally {setBusy('');}
  };
  const connect = async (kind = 'solstice') => {
    const child = window.open('about:blank', 'vwb-integration-auth', 'popup,width=560,height=720');
    if (!child) {setError('Allow the sign-in popup, then choose Connect again.'); return;}
    popup.current = child; popupStarted.current = Date.now(); setBusy('connect'); setError('');
    clearInterval(popupTimer.current);
    popupTimer.current = setInterval(() => {
      if (popup.current !== child) {clearInterval(popupTimer.current); return;}
      if (child.closed || Date.now() - popupStarted.current > 300000) {
        clearInterval(popupTimer.current); popup.current = null; setBusy('');
        (kind === 'figma' ? api('/api/integrations/refresh', {method: 'POST', body: '{}'}).then(refresh) : refresh()).catch(e => setError(e.message));
      }
    }, 1000);
    try {
      const result = await api(kind === 'figma' ? '/api/integrations/figma/connect' : '/api/integrations/connect', {method: 'POST', body: '{}'});
      if (!result.authorization_url) throw new Error('The sign-in address was not returned. Try connecting again.');
      child.location.href = result.authorization_url;
    } catch (e) {child.close(); popup.current = null; clearInterval(popupTimer.current); setBusy(''); setError(e.message);}
  };
  const items = catalog?.items || [], count = selected.length;
  const chosen = item => selected.some(r => r.id === item.id);
  return html`<div className="integration-control" ref=${shell}>
    <${Tooltip} content="Plugins and MCP connections"><button ref=${trigger} className="integration-trigger" aria-label="Plugins and MCP connections" aria-expanded=${open} disabled=${disabled} onClick=${() => setOpen(!open)}><${Icon} name="library" size=${14}/><span>${count ? `${count} selected` : 'Plugins'}</span><${Icon} name="chevron-down" size=${12}/></button><//>
    ${open && ReactDOM.createPortal(html`<section className="integration-popover" style=${position} aria-label="Plugins and MCP connections"><div className="integration-popover-heading"><h3>Plugins and MCP</h3><span className="caption">For your next message</span></div>
      ${error && html`<${MessageBar} tone="error">${error}<//>`}
      <div className="integration-options">${items.map(item => html`<button className="integration-option" key=${item.id} aria-pressed=${chosen(item)} onClick=${() => onToggle({id: item.id, kind: 'plugin', name: item.name, description: item.description})}>
        <span className="integration-checkbox"><${Icon} name=${chosen(item) ? 'check' : 'plus'} size=${13}/></span><span><strong>${item.name}<span>${item.kind === 'mcp' ? 'MCP' : 'Plugin'}</span></strong><span>${item.description}</span><span className=${`integration-state ${item.connected ? 'available' : ''}`}><${Icon} name=${item.connected ? 'check' : 'info'} size=${12}/>${integrationState(item)}${item.available && item.tools_count != null ? ` · ${item.tools_count} read tools` : ''}</span></span></button>`)}
        ${!catalog && !error && html`<p className="navigator-empty">Reading connections…</p>`}${catalog && !items.length && html`<p className="navigator-empty">No integrations are configured.</p>`}</div>
      <p className="integration-help">Selections guide the agent. A disconnected integration still needs setup before its tools can run.</p><button className="integration-manage" onClick=${showManager}>Manage connections<${Icon} name="chevron-right" size=${14}/></button></section>`, document.body)}
    ${manage && ReactDOM.createPortal(html`<div className="integration-scrim" onPointerDown=${event => {if (event.target === event.currentTarget && !busy) setManage(false);}}><section className="integration-manager" role="dialog" aria-modal="true" aria-labelledby="integration-manager-title" ref=${manager}>
      <header><div><h2 id="integration-manager-title">Plugins and connections</h2><p>Connect the tools available to your workbench.</p></div><${Tooltip} content="Close connections"><${Button} variant="text" iconOnly aria-label="Close connections" onClick=${() => setManage(false)}><${Icon} name="x" size=${16}/><//><//></header>
      <div className="integration-manager-status" aria-live="polite">${error ? html`<${MessageBar} tone="error">${error}<//>` : busy ? html`<${MessageBar} tone="progress">${busy === 'configure' ? 'Saving public settings' : busy === 'refresh' ? 'Checking available tools' : busy === 'disconnect' ? 'Disconnecting' : 'Opening sign-in'}…<//>` : html`<${MessageBar} tone="info">Solstice connects the gateway. Figma has a separate sign-in when its tools are available.<//>`}</div>
      <div className="integration-manager-body"><div className="integration-cards">${items.map(item => html`<article key=${item.id}><div><h3>${item.name}</h3><span className=${`integration-state ${item.connected ? 'available' : ''}`}>${integrationState(item)}</span></div><p>${item.description}</p>${item.message && html`<p className="caption">${item.message}</p>`}${item.tools_count != null && html`<span className="caption">${item.tools_count} available read tools</span>`}${item.id === 'figma' && item.available && !item.connected && html`<${Button} variant="secondary" size="sm" disabled=${!!busy} onClick=${() => connect('figma')}>Connect Figma<//>`}</article>`)}</div>
      <div className="integration-connection-actions"><${Button} variant="secondary" size="sm" disabled=${!!busy || !catalog?.config?.client_id || !catalog?.config?.gateway_url} onClick=${() => connect('solstice')}>Connect Solstice<//><${Button} variant="text" size="sm" disabled=${!!busy} onClick=${() => mutate('refresh')}>Check tools<//><${Button} variant="text" size="sm" disabled=${!!busy || !catalog?.config?.client_id && !items.some(i => ['connected','connecting','error'].includes(i.state))} onClick=${() => mutate('disconnect')}>Disconnect<//></div>
      <details className="integration-settings" open=${!catalog?.config?.client_id}><summary>Connection settings</summary><p>These are public application settings. Your sign-in tokens stay on the host.</p>
        <form onSubmit=${event => {event.preventDefault(); mutate('configure', publicIntegrationConfig(config));}}>
          ${[['gateway_url','Gateway URL','url'],['client_id','Public application client ID','text'],['redirect_uri','Callback URL','url']].map(([key,label,type]) => html`<label key=${key}><span>${label}</span><input type=${type} required value=${config[key]} disabled=${!!busy} onChange=${e => setConfig(previous => ({...previous,[key]:e.target.value}))}/></label>`)}
          <label><span>Allowed read tools</span><textarea value=${config.read_tools} rows="3" placeholder="One tool name per line" disabled=${!!busy} onChange=${e => setConfig(previous => ({...previous,read_tools:e.target.value}))}/></label>
          <p className="caption">${catalog?.setup?.client_registration_required ? 'An OAuth application must be registered with the exact callback URL before connecting.' : 'Use the settings supplied by your workspace administrator.'}</p>
          <${Button} variant="secondary" size="sm" type="submit" disabled=${!!busy}>Save settings<//></form></details></div>
    </section></div>`, document.body)}
  </div>`;
}
