import htm from './vendor/htm.mjs';

const {createElement, useState, useEffect, useRef, useId} = window.React;
const html = htm.bind(createElement);
const {Button, Icon, Tooltip, TextField, Checkbox} = window.Eeva;
const effortNames = {low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max', ultra: 'Ultra'};
const contextName = value => value === 1000000 ? '1M' : value === 300000 ? '300K' : `${Math.round(value / 1000)}K`;
const post = body => ({method: 'POST', body: JSON.stringify(body)});
const focusable = 'button:not([disabled]), input:not([disabled]), [tabindex="0"]';

function withModel(catalog, model) {
  if (!model) return catalog;
  const exists = catalog.models.some(item => item.id === model.id);
  return {...catalog, models: exists ? catalog.models.map(item => item.id === model.id ? model : item) : [...catalog.models, model]};
}

function CloseButton({label, onClick}) {
  return html`<${Tooltip} content=${label}><${Button} variant="text" iconOnly aria-label=${label} onClick=${onClick}>
    <${Icon} name="x" size=${16}/><//><//>`;
}

function selectionFor(model, previous, customModel) {
  const capabilities = model.capabilities || {};
  const contexts = capabilities.contexts || [], efforts = capabilities.efforts || [];
  return {model_id: model.id,
    context_tokens: contexts.includes(previous?.context_tokens) ? previous.context_tokens : contexts.at(-1) ?? null,
    effort: efforts.includes(previous?.effort) ? previous.effort : efforts.at(-1) ?? null,
    fast: !!capabilities.fast && !!previous?.fast,
    ultra_fast: !!capabilities.ultra_fast && !!previous?.ultra_fast,
    custom_model: model.id === 'custom_openrouter' ? customModel || previous?.custom_model || model.canonical_id || null : null};
}

function SettingToggle({label, description, checked, supported, busy, onChange}) {
  return html`<div className="mc-toggle-row">
    <div><span>${label}</span><p className="mc-caption">${description}</p></div>
    <button type="button" role="switch" aria-label=${label} aria-checked=${!!checked} disabled=${!supported || busy}
      className="mc-switch" onClick=${() => onChange(!checked)}><span></span></button>
  </div>`;
}

function EffortPanel({model, selection, busy, onChange}) {
  const choices = model.capabilities?.efforts || [];
  const [draft, setDraft] = useState(selection.effort);
  const draftRef = useRef(draft); draftRef.current = draft;
  useEffect(() => setDraft(selection.effort), [selection.effort, model.id]);
  const index = Math.max(0, choices.indexOf(draft));
  const commit = () => {
    if (!busy && draftRef.current !== selection.effort) onChange({effort: draftRef.current});
  };
  return html`<div className="mc-effort-panel">
    <div className="mc-setting-heading"><span>Reasoning effort</span><strong>${effortNames[draft] || 'Automatic'}</strong></div>
    ${choices.length > 1 ? html`<input className="mc-effort-slider" type="range" min="0" max=${choices.length - 1} step="1"
      value=${index} aria-label="Reasoning effort" aria-valuetext=${effortNames[draft] || draft}
      disabled=${busy} onChange=${event => {const value = choices[Number(event.target.value)]; draftRef.current = value; setDraft(value);}}
      onPointerUp=${commit} onKeyUp=${event => {if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) commit();}}
      onBlur=${commit}/>
      <div className="mc-effort-labels" aria-hidden="true">${choices.map((choice, i) => html`<span key=${choice} className=${i === index ? 'selected' : ''}>${effortNames[choice] || choice}</span>`)}</div>`
      : html`<p className="mc-caption">${choices.length ? `${effortNames[choices[0]] || choices[0]} is the supported effort for this model.` : 'This model chooses its own reasoning effort.'}</p>`}
    <p className="mc-caption mc-effort-note">Higher effort gives the model more time to reason.</p>
    <div className="mc-speed-settings">
      <${SettingToggle} label="Fast mode" supported=${!!model.capabilities?.fast} busy=${busy} checked=${selection.fast}
        description=${model.capabilities?.fast ? model.fast_note || 'Use the provider’s faster serving mode.' : 'This provider does not offer fast mode for this model.'}
        onChange=${fast => onChange({fast, ultra_fast: false})}/>
      <${SettingToggle} label="Ultra fast" supported=${!!model.capabilities?.ultra_fast} busy=${busy} checked=${selection.ultra_fast}
        description=${model.capabilities?.ultra_fast ? 'Use the provider’s ultra fast service tier.' : 'This model has no verified ultra fast service tier.'}
        onChange=${ultra_fast => onChange({ultra_fast, fast: false})}/>
    </div>
  </div>`;
}

function ModelsManager({catalog, busy, onEnabledChange}) {
  const enabled = new Set(catalog.enabled_model_ids || catalog.models.filter(model => model.enabled).map(model => model.id));
  return html`<div className="mc-manager-models">
    <p className="mc-body-note">Choose the models you want in the composer. You can change this list at any time.</p>
    <div className="mc-manager-list">${catalog.models.map(model => html`<label key=${model.id} className="mc-manager-model">
      <${Checkbox} checked=${enabled.has(model.id)} disabled=${busy} aria-label=${`Show ${model.id === 'custom_openrouter' ? 'Custom OpenRouter' : model.label} in model picker`}
        onCheckedChange=${checked => onEnabledChange(model.id, checked === true)}/>
      <span className="mc-model-info"><span>${model.id === 'custom_openrouter' ? 'Custom OpenRouter' : model.label}</span><span className="mc-caption">${model.id === 'custom_openrouter' ? 'Choose an exact provider/model ID.' : model.model}</span>
        ${!model.available && html`<span className="mc-model-unavailable">${model.unavailable_reason || 'Connect a provider to use this model.'}</span>`}</span>
      <span className="mc-provider-name">${catalog.providers.find(provider => provider.id === model.provider)?.label || model.provider}</span>
    </label>`)}</div>
    <div className="mc-manager-footnote"><${Icon} name="info" size=${14}/><span>Your selected model stays visible until you choose another.</span></div>
  </div>`;
}

function ProviderEditor({provider, busy, onSave, onClear, onReset}) {
  const [editing, setEditing] = useState(false), [key, setKey] = useState('');
  const input = useRef(null);
  useEffect(() => {if (editing) input.current?.focus(); else setKey('');}, [editing]);
  const save = async event => {
    event.preventDefault();
    if (!key.trim() || busy) return;
    if (await onSave(provider.id, key.trim())) {setKey(''); setEditing(false);}
  };
  return html`<section className="mc-provider-card" aria-label=${provider.label}>
    <div className="mc-provider-heading"><div><h3>${provider.label}</h3>
      <span className=${`mc-provider-status ${provider.configured ? 'connected' : ''}`}>
        <${Icon} name=${provider.configured ? 'check' : 'minus'} size=${12}/> ${provider.configured ? 'Configured' : 'Not configured'}</span></div>
      <${Button} variant="text" size="sm" disabled=${busy} onClick=${() => setEditing(!editing)}>${editing ? 'Cancel' : provider.configured ? 'Replace key' : 'Add key'}<//>
    </div>
    ${editing && html`<form className="mc-key-form" onSubmit=${save}>
      <label htmlFor=${`mc-key-${provider.id}`}>API key</label>
      <${TextField} ref=${input} id=${`mc-key-${provider.id}`} type="password" autoComplete="new-password"
        spellCheck=${false} value=${key} placeholder="Paste your API key" readOnly=${busy}
        onChange=${event => setKey(event.target.value)}/>
      <div className="mc-key-actions"><${Button} type="submit" variant="secondary" size="sm" disabled=${busy || !key.trim()}>${busy ? 'Saving…' : 'Save key'}<//></div>
    </form>`}
    <div className="mc-provider-source"><p className="mc-caption">${provider.source === '.env' || provider.source === 'env' || provider.source === 'environment' ? 'Configured in the host environment.' : provider.source === 'Workbench' || provider.source === 'managed' || provider.source === 'settings' ? 'Stored in private host settings.' : provider.source === 'Disabled' ? 'Connection disabled in Workbench.' : provider.configured ? 'Available on this host.' : 'Add a key to connect this provider.'}</p>
      <div className="mc-provider-actions">${provider.can_reset && html`<${Button} variant="text" size="sm" disabled=${busy} onClick=${() => onReset(provider.id)}>Use .env key<//>`}
        ${provider.configured && html`<${Button} variant="text" size="sm" disabled=${busy} onClick=${() => onClear(provider.id)}>Disconnect<//>`}</div></div>
  </section>`;
}

function ManagerDialog({catalog, busy, onClose, onEnabledChange, onProviderSave, onProviderClear, onProviderReset, initialTab = 'models'}) {
  const [tab, setTab] = useState(initialTab), dialog = useRef(null), close = useRef(null);
  const id = useId();
  useEffect(() => {
    const previous = document.activeElement;
    const root = document.getElementById('root');
    const wasInert = root?.inert;
    if (root) root.inert = true;
    close.current?.focus();
    const keydown = event => {
      if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); onClose();}
      if (event.key !== 'Tab') return;
      const items = [...(dialog.current?.querySelectorAll(focusable) || [])].filter(item => !item.closest('[hidden]'));
      if (!items.length) {event.preventDefault(); return;}
      if (event.shiftKey && document.activeElement === items[0]) {event.preventDefault(); items.at(-1).focus();}
      else if (!event.shiftKey && document.activeElement === items.at(-1)) {event.preventDefault(); items[0].focus();}
    };
    document.addEventListener('keydown', keydown, true);
    return () => {
      document.removeEventListener('keydown', keydown, true);
      if (root) root.inert = !!wasInert;
      if (previous?.isConnected) previous.focus();
    };
  }, []);
  const tabKeys = event => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'Home' ? 'models' : event.key === 'End' ? 'providers' : tab === 'models' ? 'providers' : 'models';
      setTab(next); dialog.current?.querySelector(`[data-manager-tab="${next}"]`)?.focus();
    }
  };
  return window.ReactDOM.createPortal(html`<div className="mc-modal-overlay" onPointerDown=${event => {if (event.target === event.currentTarget) onClose();}}>
    <section className="mc-manager" role="dialog" aria-modal="true" aria-labelledby=${`${id}-title`} aria-describedby=${`${id}-description`} ref=${dialog}>
      <div className="mc-manager-heading"><div><h2 id=${`${id}-title`}>Models and APIs</h2><p id=${`${id}-description`}>Set up the models available to your agent.</p></div>
        <${Tooltip} content="Close model settings"><${Button} ref=${close} variant="text" iconOnly aria-label="Close model settings" onClick=${onClose}><${Icon} name="x" size=${16}/><//><//></div>
      <div className="mc-manager-tabs" role="tablist" aria-label="Model settings" onKeyDown=${tabKeys}>
        <button type="button" role="tab" id=${`${id}-models-tab`} aria-selected=${tab === 'models'} aria-controls=${`${id}-models`} tabIndex=${tab === 'models' ? 0 : -1} data-manager-tab="models" onClick=${() => setTab('models')}>Models</button>
        <button type="button" role="tab" id=${`${id}-providers-tab`} aria-selected=${tab === 'providers'} aria-controls=${`${id}-providers`} tabIndex=${tab === 'providers' ? 0 : -1} data-manager-tab="providers" onClick=${() => setTab('providers')}>Provider APIs</button>
      </div>
      <div className="mc-manager-body">
        ${tab === 'models' ? html`<div role="tabpanel" id=${`${id}-models`} aria-labelledby=${`${id}-models-tab`}><${ModelsManager} catalog=${catalog} busy=${busy} onEnabledChange=${onEnabledChange}/></div>`
          : html`<div role="tabpanel" id=${`${id}-providers`} aria-labelledby=${`${id}-providers-tab`}><p className="mc-body-note">Keys stay on the host and are never shown again after you save them.</p>
            <div className="mc-provider-list">${catalog.providers.map(provider => html`<${ProviderEditor} key=${provider.id} provider=${provider} busy=${busy} onSave=${onProviderSave} onClear=${onProviderClear} onReset=${onProviderReset}/>`)}
            </div></div>`}
      </div>
      <div className="mc-manager-footer"><span className="mc-caption">Changes apply to the next agent turn.</span><${Button} variant="secondary" size="sm" onClick=${onClose}>Done<//></div>
    </section></div>`, document.body);
}

export function ModelControl({api, requestId, onNotice}) {
  const [catalog, setCatalog] = useState(null), [selection, setSelection] = useState(null);
  const [open, setOpen] = useState(false), [subview, setSubview] = useState(null), [manager, setManager] = useState(null);
  const [busy, setBusy] = useState(false), [loadError, setLoadError] = useState(false);
  const [customText, setCustomText] = useState('openrouter/'), [customError, setCustomError] = useState('');
  const [position, setPosition] = useState({left: 16, bottom: 40, maxHeight: 480});
  const trigger = useRef(null), menu = useRef(null), submenu = useRef(null), subTrigger = useRef(null), customInput = useRef(null);
  const scope = useRef(requestId), serial = useRef(0); scope.current = requestId;
  const id = useId();
  const report = error => onNotice?.({tone: 'error', text: error.message || 'Model settings could not be saved. Try again.'});
  useEffect(() => {
    let active = true;
    serial.current++; setBusy(false); setSelection(null); setOpen(false); setSubview(null); setManager(null); setLoadError(false);
    const fetch = async () => {
      try {
        const [nextCatalog, current] = await Promise.all([api('/api/models'), requestId ? api(`/api/requests/${encodeURIComponent(requestId)}/model`) : Promise.resolve(null)]);
        if (active) {
          const resolved = current?.model || nextCatalog.selected_model;
          setCatalog(withModel(nextCatalog, resolved));
          setSelection(current?.selection || nextCatalog.default_selection);
        }
      } catch (error) {if (active) {setLoadError(true); report(error);}}
    };
    fetch(); return () => {active = false;};
  }, [api, requestId]);

  const updatePosition = () => {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect) return;
    const width = Math.min(300, window.innerWidth - 32), bottom = Math.max(16, window.innerHeight - rect.top + 8);
    setPosition({left: Math.max(16, Math.min(rect.left, window.innerWidth - width - 16)), bottom, maxHeight: Math.max(160, window.innerHeight - bottom - 16)});
  };
  const closeMenu = (restore = true) => {setOpen(false); setSubview(null); setCustomError(''); if (restore) trigger.current?.focus();};
  const closeSubview = () => {setSubview(null); subTrigger.current?.focus();};
  const openSubview = (name, event) => {subTrigger.current = event.currentTarget; setSubview(name); setCustomError('');};
  useEffect(() => {
    if (!open) return;
    updatePosition();
    const dismiss = event => {
      if (!trigger.current?.contains(event.target) && !menu.current?.contains(event.target) && !submenu.current?.contains(event.target)) closeMenu(false);
    };
    const keydown = event => {
      if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); subview ? closeSubview() : closeMenu();}
    };
    window.addEventListener('resize', updatePosition); document.addEventListener('pointerdown', dismiss); document.addEventListener('keydown', keydown);
    return () => {window.removeEventListener('resize', updatePosition); document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', keydown);};
  }, [open, subview]);
  useEffect(() => {
    if (open && !subview) (menu.current?.querySelector('[aria-pressed="true"]') || menu.current?.querySelector(focusable))?.focus();
  }, [open]);
  useEffect(() => {
    if (!subview) return;
    if (subview === 'custom') {setCustomText(selection?.custom_model ? `openrouter/${selection.custom_model}` : 'openrouter/'); customInput.current?.focus();}
    else submenu.current?.querySelector(focusable)?.focus();
  }, [subview]);

  const saveSelection = async next => {
    if (busy) return false;
    const currentScope = scope.current, token = ++serial.current; setBusy(true);
    try {
      const result = currentScope ? await api(`/api/requests/${encodeURIComponent(currentScope)}/model`, post({selection: next}))
        : await api('/api/models/preferences', post({default_selection: next}));
      if (serial.current === token && scope.current === currentScope) {
        setSelection(result.selection || result.default_selection || next);
        if (!currentScope && result.models) setCatalog(withModel(result, result.selected_model));
        else if (result.model) setCatalog(previous => withModel(previous, result.model));
      }
      return true;
    } catch (error) {if (serial.current === token) report(error); return false;}
    finally {if (serial.current === token) setBusy(false);}
  };
  const changeSetting = patch => saveSelection({...selection, ...patch});
  const pickModel = model => {setSubview(null); if (model.available) saveSelection(selectionFor(model, selection));};
  const resolveCustom = async event => {
    event.preventDefault(); if (busy) return;
    const model = customText.trim();
    if (!/^openrouter\/[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_./:+-]+$/.test(model)) {setCustomError('Enter an exact model ID, such as openrouter/anthropic/claude-opus-4.6.'); return;}
    const token = ++serial.current, currentScope = scope.current; setBusy(true); setCustomError('');
    try {
      const result = await api('/api/models/resolve', post({model}));
      if (serial.current !== token || currentScope !== scope.current) return;
      if (!result.model?.available) {setCustomError(result.model?.unavailable_reason || 'This model is unavailable. Check its ID and your OpenRouter key.'); return;}
      const descriptor = {...result.model, id: 'custom_openrouter'};
      const next = selectionFor(descriptor, selection, descriptor.canonical_id || model.slice('openrouter/'.length));
      const saved = currentScope ? await api(`/api/requests/${encodeURIComponent(currentScope)}/model`, post({selection: next}))
        : await api('/api/models/preferences', post({default_selection: next}));
      if (serial.current === token && currentScope === scope.current) {
        setCatalog(previous => withModel(previous, descriptor));
        setSelection(saved.selection || saved.default_selection || next); setSubview(null); menu.current?.querySelector(focusable)?.focus();
      }
    } catch (error) {if (serial.current === token) setCustomError(error.message || 'The model could not be found. Check its ID and try again.');}
    finally {if (serial.current === token) setBusy(false);}
  };
  const changeEnabled = async (modelId, enabled) => {
    if (busy) return;
    const previous = catalog.enabled_model_ids || catalog.models.filter(model => model.enabled).map(model => model.id);
    const next = enabled ? [...new Set([...previous, modelId])] : previous.filter(id => id !== modelId);
    const token = ++serial.current; setBusy(true);
    try {
      const result = await api('/api/models/preferences', post({enabled_model_ids: next}));
      if (serial.current === token) setCatalog(previous => withModel(result, selection.model_id === 'custom_openrouter' ? previous.models.find(model => model.id === 'custom_openrouter') : result.selected_model));
    }
    catch (error) {if (serial.current === token) report(error);}
    finally {if (serial.current === token) setBusy(false);}
  };
  const saveProvider = async (provider, key, action = 'save') => {
    if (busy) return false;
    const token = ++serial.current; setBusy(true);
    try {
      await api(`/api/providers/${encodeURIComponent(provider)}`, post(action === 'save' ? {action, api_key: key} : {action}));
      const result = await api('/api/models');
      const current = scope.current ? await api(`/api/requests/${encodeURIComponent(scope.current)}/model`) : null;
      if (serial.current === token) setCatalog(withModel(result, current?.model || result.selected_model));
      return true;
    } catch (error) {if (serial.current === token) report(error); return false;}
    finally {if (serial.current === token) setBusy(false);}
  };
  const retryLoad = async () => {
    if (busy) return;
    const token = ++serial.current, currentScope = scope.current; setBusy(true);
    try {
      const [result, current] = await Promise.all([api('/api/models'), currentScope ? api(`/api/requests/${encodeURIComponent(currentScope)}/model`) : Promise.resolve(null)]);
      if (serial.current === token) {setCatalog(withModel(result, current?.model || result.selected_model)); setSelection(current?.selection || result.default_selection); setLoadError(false);}
    } catch (error) {if (serial.current === token) report(error);}
    finally {if (serial.current === token) setBusy(false);}
  };

  const selected = catalog?.models.find(model => model.id === selection?.model_id);
  const enabled = new Set(catalog?.enabled_model_ids || catalog?.models.filter(model => model.enabled).map(model => model.id) || []);
  const models = catalog?.models.filter(model => model.id !== 'custom_openrouter' && (enabled.has(model.id) || model.id === selection?.model_id)) || [];
  const label = selection?.model_id === 'custom_openrouter' ? selection.custom_model || selected?.label || 'OpenRouter model' : selected?.label || (loadError ? 'Model unavailable' : 'Models');
  const caps = selected?.capabilities || {};
  const subWidth = Math.min(300, window.innerWidth - 32);
  const roomOnRight = position.left + 300 + 8 + subWidth <= window.innerWidth - 16;
  const roomOnLeft = position.left - subWidth - 8 >= 16;
  const subLeft = roomOnRight ? position.left + 308 : roomOnLeft ? position.left - subWidth - 8 : position.left;
  const openManager = tab => {closeMenu(false); setManager(tab);};
  const menuKeys = event => {
    if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key) || !event.target.matches('[data-model-choice]')) return;
    event.preventDefault(); const rows = [...menu.current.querySelectorAll('[data-model-choice]:not([disabled])')];
    const index = rows.indexOf(event.target), next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
    rows[next]?.focus();
  };
  return html`<div className="mc-control">
    <${Tooltip} content="Choose the main agent model"><button type="button" ref=${trigger} className="mc-trigger" aria-label=${`Choose model: ${label}`} aria-haspopup="dialog" aria-expanded=${open} aria-controls=${`${id}-picker`}
      onClick=${() => {if (open) closeMenu(); else {setOpen(true); setSubview(null); updatePosition();}}}>
      <${Icon} name="sparkles" size=${13}/><span>${label}</span><${Icon} name="chevron-down" size=${12}/>
    </button><//>
    ${open && window.ReactDOM.createPortal(html`<section className="mc-picker" role="dialog" aria-label="Choose main agent model" id=${`${id}-picker`} ref=${menu}
      style=${{left: `${position.left}px`, bottom: `${position.bottom}px`, maxHeight: `${position.maxHeight}px`}} onKeyDown=${menuKeys} aria-busy=${busy}>
      <div className="mc-picker-heading"><span>Main agent</span><${Tooltip} content="Manage models and APIs"><button type="button" className="mc-expand" aria-label="Manage models and APIs" onClick=${() => openManager('models')}><${Icon} name="chevron-down" size=${14}/></button><//></div>
      ${catalog && selection ? html`<div className="mc-model-list" role="group" aria-label="Available models">
        ${models.map(model => html`<button type="button" key=${model.id} className="mc-model-row" data-model-choice="true" aria-pressed=${selection.model_id === model.id} disabled=${busy || !model.available}
          onClick=${() => pickModel(model)}><span className="mc-model-check">${selection.model_id === model.id && html`<${Icon} name="check" size=${14}/>`}</span>
          <span className="mc-model-info"><span>${model.label}</span>${!model.available && html`<span className="mc-model-unavailable">${model.unavailable_reason || 'Connect a provider in settings.'}</span>`}</span>
          <span className="mc-model-provider">${catalog.providers.find(provider => provider.id === model.provider)?.label || model.provider}</span></button>`)}
        ${(enabled.has('custom_openrouter') || selection.model_id === 'custom_openrouter') && html`<button type="button" className="mc-model-row mc-custom-row" data-model-choice="true" aria-pressed=${selection.model_id === 'custom_openrouter'} disabled=${busy} onClick=${event => openSubview('custom', event)}>
          <span className="mc-model-check">${selection.model_id === 'custom_openrouter' ? html`<${Icon} name="check" size=${14}/>` : html`<${Icon} name="plus" size=${14}/>`}</span>
          <span className="mc-model-info"><span>${selection.model_id === 'custom_openrouter' ? selection.custom_model : 'OpenRouter model…'}</span></span><${Icon} name="chevron-right" size=${13}/></button>`}
      </div>` : html`<div className="mc-loading"><p>${loadError ? 'Could not load models. Check the server and try again.' : 'Loading models…'}</p>
        ${loadError && html`<${Button} variant="secondary" size="sm" disabled=${busy} onClick=${retryLoad}>${busy ? 'Loading…' : 'Try again'}<//>`}</div>`}
      ${selected && selection && html`<div className="mc-configuration">
        <button type="button" className="mc-setting-row" disabled=${busy} aria-expanded=${subview === 'context'} onClick=${event => openSubview('context', event)}><span>Context</span><span className="mc-setting-value">${selection.context_tokens ? contextName(selection.context_tokens) : 'Provider default'}</span><${Icon} name="chevron-right" size=${13}/></button>
        <button type="button" className="mc-setting-row" disabled=${busy} aria-expanded=${subview === 'effort'} onClick=${event => openSubview('effort', event)}><span>Effort</span><span className="mc-setting-value">${effortNames[selection.effort] || 'Automatic'}${selection.ultra_fast ? ' · Ultra fast' : selection.fast ? ' · Fast' : ''}</span><${Icon} name="chevron-right" size=${13}/></button>
      </div>`}
      <div className="mc-picker-footer"><button type="button" className="mc-manage-link" onClick=${() => openManager('models')}>Models and APIs<${Icon} name="chevron-right" size=${13}/></button><p className="mc-caption">Changes apply to the next turn.</p></div>
    </section>`, document.body)}
    ${open && subview && window.ReactDOM.createPortal(html`<section className=${`mc-submenu ${!roomOnRight && !roomOnLeft ? 'mc-submenu-stacked' : ''}`} ref=${submenu} role="dialog" aria-label=${subview === 'custom' ? 'Choose an OpenRouter model' : subview === 'context' ? 'Choose context window' : 'Choose reasoning effort'}
      style=${{left: `${subLeft}px`, bottom: `${position.bottom}px`, maxHeight: `${position.maxHeight}px`}}>
      <div className="mc-submenu-heading"><button type="button" className="mc-back" onClick=${closeSubview}><${Icon} name="chevron-left" size=${14}/><span>${subview === 'custom' ? 'OpenRouter model' : subview === 'context' ? 'Context budget' : 'Effort and speed'}</span></button><${CloseButton} label="Close model option" onClick=${closeSubview}/></div>
      ${subview === 'context' ? html`<div className="mc-context-panel"><p className="mc-caption">${selected?.label} · Working context budget</p>
        <div role="group" aria-label="Context window">${[300000, 1000000].map(tokens => html`<button type="button" key=${tokens} className="mc-context-choice" aria-pressed=${selection.context_tokens === tokens} disabled=${busy || !caps.contexts?.includes(tokens)}
          onClick=${async () => {if (await changeSetting({context_tokens: tokens})) closeSubview();}}><span className="mc-model-check">${selection.context_tokens === tokens && html`<${Icon} name="check" size=${14}/>`}</span><span>${contextName(tokens)} tokens</span><span className="mc-caption">${caps.contexts?.includes(tokens) ? tokens === 1000000 ? 'Extended' : 'Focused' : 'Not supported'}</span></button>`)}</div>
        ${caps.contexts?.filter(tokens => ![300000, 1000000].includes(tokens)).map(tokens => html`<button type="button" key=${tokens} className="mc-context-choice" aria-pressed=${selection.context_tokens === tokens} disabled=${busy}
          onClick=${async () => {if (await changeSetting({context_tokens: tokens})) closeSubview();}}><span className="mc-model-check">${selection.context_tokens === tokens && html`<${Icon} name="check" size=${14}/>`}</span><span>Use model limit (${contextName(tokens)})</span></button>`)}
        <p className="mc-caption">Sets a working budget within this model’s actual context limit. Earlier messages remain in your conversation.</p></div>`
        : subview === 'effort' ? html`<${EffortPanel} key=${selected?.id} model=${selected} selection=${selection} busy=${busy} onChange=${changeSetting}/>`
        : html`<form className="mc-custom-form" onSubmit=${resolveCustom}><label htmlFor=${`${id}-custom`}>Exact model ID</label>
          <${TextField} id=${`${id}-custom`} ref=${customInput} value=${customText} onChange=${event => {setCustomText(event.target.value); setCustomError('');}} readOnly=${busy} error=${!!customError} autoComplete="off" spellCheck=${false} aria-describedby=${`${id}-custom-help`}/>
          <p className="mc-caption" id=${`${id}-custom-help`}>Use openrouter/ followed by the provider and model name.</p>
          ${customError && html`<p className="mc-field-error" role="alert">${customError}</p>`}
          <${Button} type="submit" variant="secondary" size="sm" disabled=${busy || !customText.trim()}>${busy ? 'Checking model…' : 'Use model'}<//>
        </form>`}
    </section>`, document.body)}
    ${manager && catalog && html`<${ManagerDialog} key=${manager} catalog=${catalog} busy=${busy} initialTab=${manager} onClose=${() => {setManager(null); trigger.current?.focus();}} onEnabledChange=${changeEnabled}
      onProviderSave=${saveProvider} onProviderClear=${provider => saveProvider(provider, null, 'clear')} onProviderReset=${provider => saveProvider(provider, null, 'reset')}/>`}
  </div>`;
}
