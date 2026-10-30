import htm from './vendor/htm.mjs';

const {createElement, useEffect, useRef, useState} = window.React;
const html = htm.bind(createElement);
let nextPickerId = 0;
const STORAGE_KEY = 'vwb-composer-preferences';
const MODES = ['ask', 'storyboard', 'agentic'];
export const MODE_HELP = 'UI preview only. Ask, Storyboard and Agentic do not alter execution or tool actions yet.';
export const RENDERERS = [
  {id: 'remotion', label: 'Remotion', scope: '', description: 'Text, captions and shapes in flexible video layouts.'},
  {id: 'browser', label: 'React / HTML + SVG', scope: '', description: 'Text, captions and precise SVG graphics.'},
  {id: 'gsap', label: 'React / HTML + GSAP', scope: '', description: 'Animated text, captions and vector graphics.'},
  {id: 'canvas2d', label: 'Canvas 2D', scope: '', description: 'Shapes only; no text or captions.'},
];
// Only these trusted IDs may reach message instructions. Catalog text is display-only.
export const TARGETS = [...RENDERERS.map(row => ({...row, kind: 'canvas_renderer'})),
  {id: 'after_effects', label: 'After Effects', kind: 'native_project',
    description: 'Editable After Effects project; render and import video.',
    scope: 'Edits stay in After Effects; no automatic canvas conversion.'},
];

export function composerPreference(value) {
  return {
    mode: MODES.includes(value?.mode) ? value.mode : 'agentic',
    renderers: TARGETS.filter(row => Array.isArray(value?.renderers) && value.renderers.includes(row.id)).map(row => row.id),
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
  if (ids.includes('after_effects')) {
    const plates = ids.filter(id => id !== 'after_effects');
    return [`Canvas authoring preference: allowed target IDs: ${ids.join(', ')}. Choose one target per output; do not overlay multiple targets.`,
      plates.length ? `For vwb.canvas/2 outputs, allowed renderer IDs: ${plates.join(', ')}. Use canvas_new(renderer=...) or canvas_set_renderer.` : '',
      'For after_effects, use the native After Effects project workflow: check after_effects_status, write a .jsx with file_write, author with after_effects_run and wait for completion with after_effects_job. Render the saved project with after_effects_render and verify completion with after_effects_job; optionally import the completed video with media_register. After Effects is a native .aep project target, not a vwb.canvas/2 renderer; never pass after_effects to canvas_new or canvas_set_renderer. If setup is required, explain it and retain this preference; do not enable automation or silently substitute another target.',
    ].filter(Boolean).join(' ');
  }
  return ids.length ? `Canvas renderer preference: allowed renderer IDs: ${ids.join(', ')}. Choose one renderer per output from these options using canvas_new(renderer=...) or canvas_set_renderer; do not overlay multiple renderers.` : '';
}

export function composerMessage(value, preference) {
  return [value.trim(), rendererInstruction(preference)].filter(Boolean).join('\n\n');
}

const nonempty = (value, fallback = '') => typeof value === 'string' && value.trim() ? value.trim() : fallback;

// These catalog scope statements are already covered by the concise card copy.
// Preserve new scope text (including additions after a known statement) as text.
const COVERED_SCOPES = {
  canvas_renderer: ['Supported plates: text, captions, shape.', 'Text, captions and shapes.', 'Shapes only; no text or captions.'],
  native_project: ['Separate native project. No automatic vwb.canvas/2 conversion, renderer swap or embedded AE editor; layers and effects stay editable in After Effects.'],
};
function extraScope(value, target) {
  let extra = nonempty(value);
  for (const covered of [target.description, target.scope, ...COVERED_SCOPES[target.kind]].filter(Boolean)) {
    if (extra.toLowerCase().startsWith(covered.toLowerCase())) extra = extra.slice(covered.length).trim();
  }
  return extra;
}

const SETUP_REASONS = new Map([
  ["Check after_effects_status for this host's installation and enablement.", 'Check installation and automation in Manage connections.'],
  ['The native After Effects bridge requires macOS.', 'Requires macOS.'],
  ['After Effects was not found. Install it or choose its app path in Manage connections.', 'Install After Effects, then check Manage connections.'],
  ['A person must enable After Effects automation in Plugins and MCPs.', 'Enable automation in Plugins and MCPs → Manage connections.'],
  ['An active or unresolved script blocks new authoring.', 'Finish or resolve the previous script before starting another.'],
  ['Authoring is configured; select an installed aerender to render saved projects.', 'Set up the After Effects renderer in Manage connections.'],
  ['Native automation is configured. Adobe licensing, script file access and macOS consent are verified only by execution.', 'License and app permissions are checked when a job runs.'],
]);
function targetAvailability(row, native) {
  const value = row?.availability;
  const status = typeof value === 'string' ? value : typeof value?.status === 'string' ? value.status : '';
  const labels = {available: 'Available', ready: 'Available', setup_required: 'Setup required', unavailable: 'Currently unavailable'};
  const reason = nonempty(value?.reason, nonempty(value?.message));
  const label = native && ['available', 'ready'].includes(status) ? 'Configured' :
    Object.hasOwn(labels, status) ? labels[status] : (native ? 'Setup not checked' : '');
  return {label, detail: !label ? '' : SETUP_REASONS.get(reason) || reason || (native ?
    label === 'Configured' ? 'License and app permissions are checked when a job runs.' : 'Check installation and automation in Manage connections.' : ''),
  };
}

// Prefer authoring targets while retaining compatibility with older servers.
// Availability is information, never permission or a reason to discard a preference.
export function rendererOptions(catalog) {
  const rows = Array.isArray(catalog?.targets) ? catalog.targets : Array.isArray(catalog?.renderers) ? catalog.renderers : [];
  return TARGETS.map(fallback => {
    const row = rows.find(item => item?.id === fallback.id);
    return {...fallback, label: nonempty(row?.label, fallback.label), extraScope: extraScope(row?.scope, fallback),
      availability: targetAvailability(row, fallback.kind === 'native_project')};
  });
}

export function ComposerControls({api, preference, onChange, disabled = false, requestId = null}) {
  const [catalog, setCatalog] = useState(null), [catalogState, setCatalogState] = useState('loading');
  const [draft, setDraft] = useState(null), [query, setQuery] = useState('');
  const dialog = useRef(null), search = useRef(null), trigger = useRef(null);
  const session = useRef(null), current = useRef(null);
  const [id] = useState(() => `composer-target-${++nextPickerId}`);
  const choice = composerPreference(preference), signature = JSON.stringify(choice);
  current.current = {requestId, signature, disabled};
  const openedSession = session.current;
  const open = draft !== null && openedSession?.requestId === requestId && openedSession?.signature === signature;
  // Reused instances should receive requestId. Keyed callers remain compatible;
  // capturing their opening callback also prevents a late Apply being redirected.
  useEffect(() => {
    if (!open) {session.current = null; setDraft(null);}
  }, [open]);
  useEffect(() => () => {session.current = null;}, []);
  useEffect(() => {
    if (!open) return;
    let live = true;
    setCatalog(null); setCatalogState('loading');
    api('/api/canvas-renderers').then(result => {
      if (!Array.isArray(result?.targets) && !Array.isArray(result?.renderers)) throw new Error('Missing target catalog');
      if (live) {setCatalog(result); setCatalogState('ready');}
    }).catch(() => {if (live) setCatalogState('failed');});
    return () => {live = false;};
  }, [api, open]);
  useEffect(() => {
    if (!open) return;
    const node = dialog.current, prior = trigger.current || document.activeElement;
    // The native top layer escapes the composer's stacking context and makes the background inert.
    node.showModal(); search.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      node.close(); document.body.style.overflow = previousOverflow;
      if (prior?.isConnected) prior.focus();
    };
  }, [open]);
  const options = rendererOptions(catalog);
  const instruction = rendererInstruction(choice);
  const label = !choice.renderers.length ? 'Auto' : choice.renderers.length === 1 ? options.find(row => row.id === choice.renderers[0]).label : `${choice.renderers.length} selected`;
  const matches = options.filter(row => [row.label, row.id, row.scope, row.description, row.extraScope,
    row.kind === 'native_project' ? 'native project' : 'canvas renderer', row.availability.label, row.availability.detail]
    .join(' ').toLowerCase().includes(query.trim().toLowerCase()));
  const hiddenSelections = (draft || []).filter(value => !matches.some(row => row.id === value)).length;
  const close = () => {session.current = null; setDraft(null);};
  const toggle = target => {if (!disabled) setDraft(previous => previous.includes(target) ? previous.filter(value => value !== target) : [...previous, target]);};
  return html`<div className="composer-preferences">
    <div className="composer-preferences-row">
      <label className="composer-mode" title=${MODE_HELP}><span className="visually-hidden">Composer mode (UI preview only)</span>
        <select value=${choice.mode} disabled=${disabled} aria-describedby=${`${id}-mode-help`} onChange=${event => onChange({...choice, mode: event.target.value})}>
          <option value="ask">Ask</option><option value="storyboard">Storyboard</option><option value="agentic">Agentic</option>
        </select><span className="composer-preview-label">UI preview only</span>
      </label>
      <span id=${`${id}-mode-help`} className="visually-hidden">${MODE_HELP}</span>
      <button type="button" ref=${trigger} className="composer-target-trigger" disabled=${disabled}
        aria-haspopup="dialog" aria-expanded=${open} aria-controls=${`${id}-dialog`}
        onClick=${() => {if (!disabled) {
          session.current = {requestId, signature, choice, onChange}; setDraft([...choice.renderers]); setQuery('');
        }}}>Canvas · ${label}</button>
    </div>
    ${open && html`<dialog ref=${dialog} id=${`${id}-dialog`} className="composer-target-dialog" tabIndex="-1"
      aria-modal="true" aria-labelledby=${`${id}-title`} aria-describedby=${`${id}-help`}
      onCancel=${event => {event.preventDefault(); close();}}
      onKeyDown=${event => {
        if (event.key === 'Escape') {event.preventDefault(); event.stopPropagation(); close(); return;}
        if (event.key !== 'Tab') return;
        const nodes = [...event.currentTarget.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]')];
        const first = nodes[0], last = nodes.at(-1), active = document.activeElement;
        if (!nodes.length || !nodes.includes(active) || (event.shiftKey ? active === first : active === last)) {
          event.preventDefault(); (event.shiftKey ? last : first)?.focus();
          if (!nodes.length) event.currentTarget.focus();
        }
      }}>
      <header className="composer-target-heading"><div><h2 id=${`${id}-title`}>Choose a canvas</h2>
        <p id=${`${id}-help`}>Choose one or more options. The agent uses one per output.</p></div>
        <button type="button" className="composer-target-close" aria-label="Close canvas picker" onClick=${close}>×</button>
      </header>
      <div className="composer-target-search"><label htmlFor=${`${id}-search`}>Search canvas options</label>
        <input ref=${search} id=${`${id}-search`} type="search" value=${query} placeholder="Search by name or capability…"
          aria-controls=${`${id}-results`} onChange=${event => setQuery(event.target.value)}/>
        <p role="status">${matches.length} ${matches.length === 1 ? 'option' : 'options'}${hiddenSelections ? ` · ${hiddenSelections} selected outside this search` : ''}</p>
      </div>
      <div className="composer-target-scroll">
        <fieldset disabled=${disabled} className="composer-target-choices" aria-describedby=${`${id}-help`}>
          <legend className="visually-hidden">Preferred canvas targets</legend>
          <label className=${`composer-target-card composer-target-auto ${!draft.length ? 'is-selected' : ''}`}>
            <input type="checkbox" checked=${!draft.length} onChange=${() => {if (!disabled) setDraft([]);}}/>
            <span><strong>Auto</strong><span>Let the agent choose.</span></span>
          </label>
          <div id=${`${id}-results`} className="composer-target-grid">
            ${matches.map(row => html`<label key=${row.id} className=${`composer-target-card ${draft.includes(row.id) ? 'is-selected' : ''}`}>
              <input type="checkbox" checked=${draft.includes(row.id)} onChange=${() => toggle(row.id)}
                aria-labelledby=${`${id}-${row.id}-name`} aria-describedby=${`${id}-${row.id}-detail`}/>
              <span><strong id=${`${id}-${row.id}-name`}>${row.label}</strong>
                <span id=${`${id}-${row.id}-detail`}><span>${row.description}</span>
                  ${row.scope && html`<span className="composer-target-scope">${row.scope}</span>`}
                  ${row.extraScope && html`<span className="composer-target-scope">${row.extraScope}</span>`}
                  ${row.availability.label && html`<span className="composer-target-availability"><b>${row.availability.label}</b>${row.availability.detail ? ` · ${row.availability.detail}` : ''}</span>`}
                </span>
              </span>
            </label>`)}
          </div>
        </fieldset>
        ${!matches.length && html`<div className="composer-target-empty"><strong>No canvas options match “${query.trim()}”.</strong>
          <p>Try a name like After Effects or a capability like text or shapes. Your selections are kept.</p>
          <button type="button" onClick=${() => {setQuery(''); search.current?.focus();}}>Clear search</button></div>`}
        <p className="composer-catalog-note">${catalogState === 'failed' ? 'Catalog unavailable. Showing built-in options; runtime availability is not verified.' :
          catalogState === 'loading' ? 'Loading catalog. Built-in options remain selectable.' : 'Runtime readiness is checked when used.'} Selecting a native app does not enable automation.</p>
      </div>
      <footer className="composer-target-actions"><span role="status">${draft.length ? `${draft.length} selected` : 'Auto selected'}</span>
        <button type="button" onClick=${close}>Cancel</button>
        <button type="button" className="composer-target-apply" disabled=${disabled} onClick=${() => {
          if (session.current !== openedSession || !openedSession || current.current.disabled ||
              current.current.requestId !== openedSession.requestId || current.current.signature !== openedSession.signature) return;
          openedSession.onChange(composerPreference({...openedSession.choice, renderers: draft})); close();
        }}>Apply</button>
      </footer>
    </dialog>`}
    ${instruction && html`<details className="composer-instruction"><summary>Canvas instruction added to your message</summary><p>${instruction}</p></details>`}
  </div>`;
}
