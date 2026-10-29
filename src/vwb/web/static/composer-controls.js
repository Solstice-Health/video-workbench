import htm from './vendor/htm.mjs';

const {createElement, useEffect, useState} = window.React;
const html = htm.bind(createElement);
const STORAGE_KEY = 'vwb-composer-preferences';
const MODES = ['ask', 'storyboard', 'agentic'];
export const MODE_HELP = 'UI preview only. Ask, Storyboard and Agentic do not alter execution or tool actions yet.';
export const RENDERERS = [
  {id: 'remotion', label: 'Remotion'},
  {id: 'browser', label: 'React / HTML + SVG'},
  {id: 'gsap', label: 'React / HTML + GSAP'},
  {id: 'canvas2d', label: 'Canvas 2D'},
];

export function composerPreference(value) {
  return {
    mode: MODES.includes(value?.mode) ? value.mode : 'agentic',
    renderers: RENDERERS.filter(row => Array.isArray(value?.renderers) && value.renderers.includes(row.id)).map(row => row.id),
  };
}

export function readComposerPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return {};
    return Object.fromEntries(Object.entries(saved).map(([id, value]) => [id, composerPreference(value)]));
  } catch (_) {return {};}
}

export function saveComposerPreferences(preferences) {
  try {localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));} catch (_) { /* Keep the in-memory choice. */ }
}

export function rendererInstruction(preference) {
  const ids = composerPreference(preference).renderers;
  return ids.length ? `Canvas renderer preference: allowed renderer IDs: ${ids.join(', ')}. Choose one renderer per output from these options using canvas_new(renderer=...) or canvas_set_renderer; do not overlay multiple renderers.` : '';
}

export function composerMessage(value, preference) {
  return [value.trim(), rendererInstruction(preference)].filter(Boolean).join('\n\n');
}

// Catalog labels and descriptions may evolve independently of the composer.
// The catalog is a list of choices, not a runtime readiness check.
export function rendererOptions(catalog) {
  return RENDERERS.map(fallback => {
    const row = catalog?.renderers?.find?.(item => item?.id === fallback.id);
    return {...fallback, label: typeof row?.label === 'string' && row.label.trim() ? row.label : fallback.label,
      description: typeof row?.description === 'string' ? row.description : ''};
  });
}

export function ComposerControls({api, preference, onChange, disabled = false}) {
  const [catalog, setCatalog] = useState(null), [catalogFailed, setCatalogFailed] = useState(false);
  useEffect(() => {
    let live = true;
    api('/api/canvas-renderers').then(result => {
      if (!Array.isArray(result?.renderers)) throw new Error('Missing renderer catalog');
      if (live) {setCatalog(result); setCatalogFailed(false);}
    }).catch(() => {if (live) setCatalogFailed(true);});
    return () => {live = false;};
  }, [api]);
  const choice = composerPreference(preference), options = rendererOptions(catalog);
  const instruction = rendererInstruction(choice);
  const label = !choice.renderers.length ? 'Auto' : choice.renderers.length === 1 ? options.find(row => row.id === choice.renderers[0]).label : `${choice.renderers.length} allowed`;
  const toggle = id => onChange({...choice, renderers: choice.renderers.includes(id) ? choice.renderers.filter(value => value !== id) : [...choice.renderers, id]});
  return html`<div className="composer-preferences">
    <div className="composer-preferences-row">
      <label className="composer-mode" title=${MODE_HELP}><span className="visually-hidden">Composer mode (UI preview only)</span>
        <select value=${choice.mode} disabled=${disabled} aria-describedby="composer-mode-help" onChange=${event => onChange({...choice, mode: event.target.value})}>
          <option value="ask">Ask</option><option value="storyboard">Storyboard</option><option value="agentic">Agentic</option>
        </select><span className="composer-preview-label">UI preview only</span>
      </label>
      <span id="composer-mode-help" className="visually-hidden">${MODE_HELP}</span>
      <details className="composer-renderers" onKeyDown=${event => {
        if (event.key === 'Escape' && event.currentTarget.open) {
          event.preventDefault(); event.stopPropagation(); event.currentTarget.open = false;
          event.currentTarget.querySelector('summary')?.focus();
        }
      }}>
        <summary title="Choose allowed canvas renderers; one renderer per output">Canvas · ${label}</summary>
        <div className="composer-renderer-options">
          <fieldset disabled=${disabled} aria-describedby="composer-renderer-help">
            <legend>Allowed canvas renderers</legend>
            <p id="composer-renderer-help">Choose Auto or multiple allowed options. The agent chooses one renderer per output, not a simultaneous overlay.</p>
            <label><input type="checkbox" checked=${!choice.renderers.length} onChange=${() => onChange({...choice, renderers: []})}/><span>Auto <small>No added renderer preference</small></span></label>
            ${options.map(row => html`<label key=${row.id} title=${row.description || undefined}><input type="checkbox" checked=${choice.renderers.includes(row.id)} onChange=${() => toggle(row.id)}/><span>${row.label}<small>${row.id}</small></span></label>`)}
          </fieldset>
          <p className="composer-catalog-note">${catalogFailed ? 'Catalog unavailable. Showing built-in options; runtime availability is not verified.' : 'Renderer choices are preferences. Runtime availability is not verified here.'}</p>
        </div>
      </details>
    </div>
    ${instruction && html`<details className="composer-instruction"><summary>Renderer instruction added to your message</summary><p>${instruction}</p></details>`}
  </div>`;
}
