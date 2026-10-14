import htm from './vendor/htm.mjs';
import {triggerAt, matchingOptions, toContext} from './composer-picker.js';
import {ModelControl} from './model-control.js';
import {WorkspaceExplorer, WorkspaceCanvas, initialCanvas, canvasReducer, importedTabs} from './workspace-canvas.js';
import {IntegrationsControl} from './integrations-control.js';
import {COMPACT_WIDTH, paneLayout, panePreferences} from './pane-layout.js';

// Eeva and React are local, versioned assets. No network service is needed to open the UI.
const {createElement, useState, useEffect, useRef, useCallback} = window.React;
const html = htm.bind(createElement);
const {Surface, Button, Icon, Tooltip, SolsticeMark, UserMessage, AgentMessage,
  Composer, MessageBar, Tabs, TabList, Tab, TabPanel} = window.Eeva;
const number = value => value == null ? 'Not reported' : new Intl.NumberFormat().format(value);
const shortNumber = value => value == null ? '—' : value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
const time = value => value ? new Date(value).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : '';
const date = value => value ? new Date(value).toLocaleDateString([], {month: 'short', day: 'numeric'}) : '';
const stamp = value => typeof value === 'number' ? value * 1000 : Date.parse(value || '') || 0;
const plain = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const title = value => (value || 'Untitled conversation').replace(/_/g, ' ').replace(/\.mp4$/i, '').replace(/\bFinal Video.*$/i, '').trim();

function IconButton({name, label, onClick, disabled, ...rest}) {
  return html`<${Tooltip} content=${label}><${Button} variant="text" iconOnly aria-label=${label}
    onClick=${onClick} disabled=${disabled} ...${rest}><${Icon} name=${name} size=${16}/><//><//>`;
}

function ReferenceTags({context}) {
  const labels = [...(context?.skills || []).map(id => `/features ${id}`), ...(context?.folders || []).map(id => `@work/${id === '.' ? '' : `${id}/`}`), ...(context?.plugins || []).map(id => `Plugin: ${id}`)];
  return labels.length ? html`<div className="message-references" aria-label="Message references">${labels.map(label => html`<span key=${label}>${label}</span>`)}</div>` : null;
}

function blocks(markdown) {
  const out = [];
  const lines = String(markdown || '').split('\n');
  for (let i = 0; i < lines.length;) {
    const line = lines[i].trim();
    if (!line) { i++; continue; }
    if (/^```/.test(line)) {
      const code = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) code.push(lines[i]);
      out.push({type: 'text', text: code.join('\n')}); i++; continue;
    }
    if (/^\|/.test(line) && i + 1 < lines.length && /^\|?[\s:|-]+\|/.test(lines[i + 1])) {
      const cells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
      const columns = cells(line).map(label => ({label})); const rows = []; i += 2;
      while (i < lines.length && /^\|/.test(lines[i].trim())) rows.push(cells(lines[i++]));
      out.push({type: 'table', columns, rows}); continue;
    }
    const list = /^[-*+]\s/.test(line) ? 'bullets' : /^\d+[.)]\s/.test(line) ? 'numbered' : null;
    if (list) {
      const items = [], regex = list === 'bullets' ? /^[-*+]\s+/ : /^\d+[.)]\s+/;
      while (i < lines.length && regex.test(lines[i].trim())) items.push(lines[i++].trim().replace(regex, ''));
      out.push({type: list, items}); continue;
    }
    const paragraph = [line.replace(/^#{1,6}\s+(.+)$/, '**$1**')]; i++;
    while (i < lines.length && lines[i].trim() && !/^(?:[-*+]\s|\d+[.)]\s|#|```|\|)/.test(lines[i].trim())) paragraph.push(lines[i++]);
    out.push({type: 'text', text: paragraph.join('\n')});
  }
  return out;
}

function uniqueEvents(events) {
  const map = new Map();
  for (const event of events || []) map.set(event.data?.id || `seq-${event.seq}`, event);
  return [...map.values()].sort((a, b) => a.seq - b.seq);
}

function ToolCall({event, settledState}) {
  const d = event.data || {}, done = d.status === 'completed', failed = d.status === 'failed' || (!done && settledState === 'failed');
  const stopped = !done && !failed && settledState === 'stopped';
  const command = d.arguments?.command || d.arguments?.cmd || d.command;
  const label = d.name || (command ? 'Bash' : 'Tool');
  return html`<details className="tool-call">
    <summary><${Icon} name=${failed ? 'circle-alert' : done ? 'check' : 'square'} size=${13}/>
      <span className="tool-name">${label.replace(/^mcp__vwb__/, '')}</span>
      <span className="tool-preview">${command || d.text || (d.arguments ? plain(d.arguments).replace(/\s+/g, ' ') : '')}</span>
      <span className="tool-state">${failed ? 'Failed' : done ? 'Done' : stopped ? 'Stopped' : 'Running'}</span><${Icon} name="chevron-down" size=${13}/>
    </summary>
    <div className="tool-detail">${d.arguments && html`<div className="section-label">Input</div><pre>${plain(d.arguments)}</pre>`}
      ${(d.output || d.text) && html`<div className="section-label">Output</div><pre>${d.output || d.text}</pre>`}
      ${d.exit_code != null && html`<p className="caption">Exit code ${d.exit_code}</p>`}</div>
  </details>`;
}

function Activity({events, working = false, started, state = 'done'}) {
  const [expanded, setExpanded] = useState(false), [allTools, setAllTools] = useState(false);
  const visible = uniqueEvents(events);
  const thoughts = visible.filter(e => e.kind === 'thinking' && e.data?.text);
  const tools = visible.filter(e => e.kind === 'tool');
  const messages = visible.filter(e => e.kind === 'message' && e.data?.text);
  if (!thoughts.length && !tools.length && !working && !messages.length) return null;
  const last = thoughts.at(-1)?.data.text || (tools.at(-1)?.data.name ? `Using ${tools.at(-1).data.name}` : 'Working on your request');
  return html`<div className="activity">
    <button className="thinking-toggle" aria-expanded=${expanded} onClick=${() => setExpanded(!expanded)}>
      <${SolsticeMark} size=${14} spinning=${working}/><span>${state === 'stopped' ? 'Stopped' : state === 'failed' ? 'Turn failed' : 'Thinking'}</span><${Icon} name=${expanded ? 'chevron-down' : 'chevron-right'} size=${13}/>
      ${working && started && html`<span className="caption">${Math.max(0, Math.floor((Date.now() - stamp(started)) / 1000))}s</span>`}
    </button>${working && html`<p className="live-thought">${last.length > 110 ? `${last.slice(0, 107)}…` : last}</p>`}
    ${expanded && html`<div className="thinking-stream"><p className="caption">Provider reasoning summaries and activity</p>
      ${thoughts.length ? thoughts.map(e => html`<p key=${e.data.id || e.seq}>${e.data.text}</p>`) : html`<p>No reasoning summary was emitted for this turn.</p>`}
      ${messages.map(e => html`<p key=${e.data.id || e.seq}>${e.data.text}</p>`)}</div>`}
    <div className="tools">${(allTools ? tools : tools.slice(-3)).map(e => html`<${ToolCall} key=${e.data.id || e.seq} event=${e} settledState=${working ? null : state}/>`)}
      ${tools.length > 3 && html`<button className="text-action tool-more" onClick=${() => setAllTools(!allTools)}>${allTools ? 'Show fewer tools' : `Show all ${tools.length} tool calls`}</button>`}</div>
    ${working && messages.at(-1) && html`<${AgentMessage} blocks=${blocks(messages.at(-1).data.text)}/>`}
  </div>`;
}

function ContextUsage({usage, active, pending, compact, busy}) {
  const [open, setOpen] = useState(false), ref = useRef(null), trigger = useRef(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = e => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const escape = e => { if (e.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown', dismiss); document.addEventListener('keydown', escape);
    return () => {document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape);};
  }, [open]);
  const ctx = usage?.context || {}, bill = usage?.billing || {};
  const percent = ctx.percent == null ? null : Math.max(0, Math.min(100, ctx.percent));
  return html`<div className="context-control" ref=${ref}>
    <${Tooltip} content="Context and token usage"><button ref=${trigger} className="context-trigger" aria-label="Context and token usage"
      aria-expanded=${open} aria-controls="context-popover" onClick=${() => setOpen(!open)}>
      <span className=${`context-pie ${percent == null ? 'unknown' : ''}`} style=${{'--used': `${percent || 0}%`}}></span>
      <span>${percent == null ? 'Context' : `${Math.round(percent)}%`}</span>
    </button><//>
    ${open && html`<section id="context-popover" className="context-popover" aria-label="Context and token usage">
      <div className="popover-heading"><h3>Context usage</h3><${IconButton} name="x" label="Close context usage" onClick=${() => {setOpen(false); trigger.current?.focus();}}/></div>
      <div className="context-total"><strong>${shortNumber(ctx.used_tokens)}</strong><span> / ${shortNumber(ctx.limit_tokens)} tokens</span></div>
      <p className="caption">${ctx.used_tokens == null ? 'This provider has not reported its context usage yet.' : `Last reported context${ctx.measured_at ? ` at ${time(ctx.measured_at)}` : ''}. Updates after each model call.`}</p>
      ${percent != null && html`<div className="usage-track" aria-hidden="true"><span style=${{width: `${percent}%`}}></span></div>`}
      ${ctx.breakdown && html`<dl className="usage-rows">
        <div><dt>Input</dt><dd>${number(ctx.breakdown.input_tokens)}</dd></div>
        <div><dt>Cached input <span className="caption">(included)</span></dt><dd>${number(ctx.breakdown.cached_input_tokens)}</dd></div>
        <div><dt>Output</dt><dd>${number(ctx.breakdown.output_tokens)}</dd></div>
        <div><dt>Reasoning <span className="caption">(included in output)</span></dt><dd>${number(ctx.breakdown.reasoning_output_tokens)}</dd></div>
      </dl>`}
      <div className="usage-billing"><h4>Conversation totals</h4><dl className="usage-rows">
        <div><dt>Input tokens</dt><dd>${number(bill.input_tokens)}</dd></div>
        <div><dt>Output tokens</dt><dd>${number(bill.output_tokens)}</dd></div>
        <div><dt>Model calls</dt><dd>${number(bill.calls)}</dd></div></dl>
        <p className="caption">Cumulative usage, including helpers. This is separate from the current context.</p></div>
      <${Button} variant="secondary" size="sm" className="compact-button" disabled=${!!active || pending || busy || !usage?.compact_available} onClick=${compact}>
        <${Icon} name="refresh" size=${14}/> ${busy ? 'Compacting…' : 'Compact conversation'}<//>
      <p className="caption">${active || pending ? 'Available after the current turn and queued messages finish.' : usage?.compact_available ? 'Keeps your thread, decisions and files. Earlier context is summarized.' : 'Available after a Codex conversation has started.'}</p>
    </section>`}
  </div>`;
}

function QuestionQueue({questions, index, setIndex, answer, setAnswer, hidden, setHidden}) {
  if (!questions.length) return null;
  const q = questions[Math.min(index, questions.length - 1)];
  return html`<section className="question-queue" aria-label="Questions from the agent">
    <div className="question-heading"><button className="question-toggle" onClick=${() => setHidden(!hidden)} aria-expanded=${!hidden}>
      <${Icon} name="circle-alert" size=${14}/><span>${q.status === 'defaulted' ? 'Review an earlier choice' : 'Your input'}</span><${Icon} name=${hidden ? 'chevron-right' : 'chevron-down'} size=${13}/></button>
      <div className="question-navigation"><span className="caption">${index + 1} of ${questions.length}</span>
        <${IconButton} name="chevron-left" label="Previous question" disabled=${index === 0} onClick=${() => setIndex(index - 1)}/>
        <${IconButton} name="chevron-right" label="Next question" disabled=${index >= questions.length - 1} onClick=${() => setIndex(index + 1)}/></div></div>
    ${!hidden && html`<h3>${q.question}</h3>${q.why && html`<p className="question-why">${q.why}</p>`}
      ${q.status === 'defaulted' && html`<p className="caption">The agent used its recommended option while waiting. You can change it.</p>`}
      <div role="group" aria-label=${q.question} className="question-options">
        ${(q.options || []).map((o, i) => html`<button key=${o.id} aria-pressed=${answer === o.id} className=${`question-option ${answer === o.id ? 'selected' : ''}`} onClick=${() => setAnswer(answer === o.id ? null : o.id)}>
          <span className="option-key">${i + 1}</span><span className="option-content"><span>${o.label || o.text || o.id}</span>
          ${o.description && html`<span className="caption">${o.description}</span>`}</span>${q.recommended === o.id && html`<span className="recommended">Recommended</span>`}
          ${answer === o.id && html`<${Icon} name="check" size=${14}/>`}</button>`)}
      </div>`}
  </section>`;
}

function OutputPanel({data, outputId, setOutputId, notice, onClose}) {
  const outputs = data?.outputs || [], output = outputs.find(o => (o.id || o.output) === outputId) || outputs[0];
  const req = data?.request;
  return html`<aside className="output-panel" aria-label="Request and outputs">
    <div className="panel-heading"><h2>Your workspace</h2><${IconButton} name="panel-right-close" label="Close workspace panel" onClick=${onClose}/></div>
    ${notice && html`<${MessageBar} tone=${notice.tone}>${notice.text}<//>`}
    <${Tabs} defaultValue="outputs" className="panel-tabs"><${TabList}><${Tab} value="outputs" count=${outputs.length}>Outputs<//><${Tab} value="details">Request<//><//>
      <${TabPanel} value="outputs" className="output-content">
        ${outputs.length ? html`<div className="output-selector" role="group" aria-label="Select output">${outputs.map((o, i) => html`<button key=${o.id || o.output} aria-pressed=${o === output} onClick=${() => setOutputId(o.id || o.output)}>
          <span>${(o.id || o.output || `Short ${i + 1}`).replace(/-/g, ' ')}</span><span className="caption">v${o.version}</span></button>`)}</div>
          <div className="preview-stage">${output.video_url ? html`<video key=${output.video_url} controls playsInline preload="metadata" poster=${output.thumbnail_url || undefined} src=${output.video_url} aria-label=${`Preview ${output.id || output.output}`} />` : html`<div className="empty-preview"><${Icon} name="file" size=${18}/><p>The preview appears after rendering.</p></div>`}</div>
          <div className="output-metadata"><span>${output.width || '—'} × ${output.height || '—'}${output.duration_s ? ` · ${output.duration_s.toFixed(1)}s` : ''}</span><span>Version ${output.version}</span></div>
          <div className="review-heading"><h3>Review</h3><span className=${`status-pill ${output.triage === 'GREEN' ? 'success' : 'required'}`}><${Icon} name=${output.triage === 'GREEN' ? 'check' : 'circle-alert'} size=${12}/>${output.triage === 'GREEN' ? 'Ready' : 'Needs review'}</span></div>
          <p className="review-explanation">${output.triage === 'GREEN' ? 'Checks are complete for this version.' : 'Check the flagged details before accepting this version.'}</p>
          <div className="review-flags">${(output.flags || []).slice(0, 4).map((f, i) => html`<details key=${f.id || i}><summary><${Icon} name="chevron-right" size=${13}/><span>${(f.kind || f.check || 'Review detail').replace(/[-_.]/g, ' ').toLowerCase()}</span></summary><p>${f.summary || f.message}</p></details>`)}</div>
          <div className="output-links">${output.review_url && html`<a className="secondary-link" href=${output.review_url} target="_blank" rel="noreferrer">Open full review <${Icon} name="arrow-up" size=${14}/></a>`}
            ${output.video_url && html`<a className="text-link" href=${output.video_url} download>Download video</a>`}</div>` : html`<div className="panel-empty"><${Icon} name="file" size=${18}/><h3>Your videos appear here</h3><p>As the agent works, you can preview each output and review its changes.</p></div>`}
      <//>
      <${TabPanel} value="details" className="request-content"><h3>What we’re making</h3><p>${req?.spec?.summary || req?.message || 'Start with a source video and tell the agent what you want to make.'}</p>
        ${req?.spec?.constraints?.length > 0 && html`<h3>Constraints</h3><ul>${req.spec.constraints.map(c => html`<li key=${c}>${c}</li>`)}</ul>`}
        <h3>Source files</h3>${req?.sources?.length ? req.sources.map(s => html`<div className="source-file" key=${s.media_id}><${Icon} name="file" size=${16}/><span>${s.name || s.original_path?.split('/').at(-1) || s.media_id}</span></div>`) : html`<p>No source attached yet.</p>`}
      <//>
    <//>
  </aside>`;
}

function App() {
  const [requests, setRequests] = useState([]), [selected, setSelected] = useState(null), [data, setData] = useState(null);
  const [text, setText] = useState(''), [drafts, setDrafts] = useState({}), [uploads, setUploads] = useState([]);
  const [references, setReferences] = useState([]), [features, setFeatures] = useState({skills: [], folders: []});
  const [picker, setPicker] = useState(null), [pickIndex, setPickIndex] = useState(0), [featuresLoading, setFeaturesLoading] = useState(false);
  const [featuresError, setFeaturesError] = useState('');
  const pickerShell = useRef(null);
  const [error, setError] = useState(''), [info, setInfo] = useState(''), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [sidebar, setSidebar] = useState(false), [outputId, setOutputId] = useState(null);
  const [navigator, setNavigator] = useState('conversations'), [navigatorOpen, setNavigatorOpen] = useState(() => window.innerWidth >= 1024);
  const [chatShown, setChatShown] = useState(true), [mobileView, setMobileView] = useState('chat');
  const [paneSizes, setPaneSizes] = useState(() => {try {return panePreferences(JSON.parse(localStorage.getItem('vwb-pane-sizes')));} catch (_) {return panePreferences();}});
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth), [resizingPane, setResizingPane] = useState(null);
  const layout = paneLayout(viewportWidth, paneSizes, navigatorOpen, chatShown);
  const [canvases, setCanvases] = useState({});
  const seenImports = useRef(new Map());
  const paneDrag = useRef(null), canvasKey = selected || 'new';
  const canvas = canvases[canvasKey] || initialCanvas();
  const dispatchCanvas = action => setCanvases(previous => ({...previous, [canvasKey]: canvasReducer(previous[canvasKey] || initialCanvas(), action)}));
  const openCanvas = tab => {dispatchCanvas({type: 'open', tab}); setMobileView('canvas'); setSidebar(false);};
  const explore = () => {setNavigator('workspace'); setNavigatorOpen(true); if (window.innerWidth < COMPACT_WIDTH) setSidebar(true);};
  const activeCanvasPath = canvas.groups.find(g => g.id === canvas.focused)?.tabs.find(t => t.id === canvas.groups.find(g => g.id === canvas.focused)?.active)?.path;
  const toggleNavigator = view => {setNavigator(view); if (window.innerWidth < COMPACT_WIDTH) setSidebar(value => view === navigator ? !value : true); else setNavigatorOpen(value => view === navigator ? !value : true);};
  const resizePane = (kind, value) => setPaneSizes(previous => ({...previous, [kind]: Math.max(layout[`${kind}Min`], Math.min(layout[`${kind}Max`], value))}));
  const finishResize = event => {
    paneDrag.current = null; setResizingPane(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const divider = kind => html`<div className=${`pane-resize-handle ${kind === 'chat' ? 'chat' : 'navigator'}-resize-handle`} role="separator"
    aria-label=${kind === 'chat' ? 'Resize chat and canvas' : 'Resize workspace sidebar'} aria-orientation="vertical"
    aria-valuemin=${layout[`${kind}Min`]} aria-valuemax=${Math.round(layout[`${kind}Max`])} aria-valuenow=${Math.round(layout[kind])}
    title="Drag to resize. Arrow keys adjust; double-click resets." tabIndex="0"
    onDoubleClick=${() => resizePane(kind, kind === 'chat' ? 360 : 196)}
    onKeyDown=${event => {const next = {ArrowLeft:layout[kind] - 16, ArrowRight:layout[kind] + 16, Home:layout[`${kind}Min`], End:layout[`${kind}Max`]}[event.key]; if(next !== undefined){event.preventDefault(); resizePane(kind, next);}}}
    onPointerDown=${event => {if(event.button !== 0) return; event.preventDefault(); event.currentTarget.focus(); paneDrag.current = {id:event.pointerId, x:event.clientX, width:layout[kind]}; event.currentTarget.setPointerCapture(event.pointerId); setResizingPane(kind);}}
    onPointerMove=${event => {const drag=paneDrag.current; if(drag?.id === event.pointerId) resizePane(kind, drag.width + event.clientX - drag.x);}}
    onPointerUp=${finishResize} onPointerCancel=${finishResize} onLostPointerCapture=${() => {paneDrag.current=null; setResizingPane(null);}}></div>`;
  useEffect(() => {try {localStorage.setItem('vwb-pane-sizes', JSON.stringify(paneSizes));} catch (_) {}}, [paneSizes]);
  const [qIndex, setQIndex] = useState(0), [answers, setAnswers] = useState({}), [questionDrafts, setQuestionDrafts] = useState({}), [questionsHidden, setQuestionsHidden] = useState(false);
  const [query, setQuery] = useState(''), [compactBusy, setCompactBusy] = useState(false), [isNearBottom, setIsNearBottom] = useState(true);
  const csrf = useRef(''), selection = useRef(null), feed = useRef(null), fileInput = useRef(null), composer = useRef(null);
  const snapshot = useRef(null), pollCount = useRef(0);
  const [narrow, setNarrow] = useState(() => window.innerWidth < COMPACT_WIDTH);
  const [mobile, setMobile] = useState(() => window.innerWidth < 640);
  useEffect(() => {
    const resize = () => {setViewportWidth(window.innerWidth); setNarrow(window.innerWidth < COMPACT_WIDTH); setMobile(window.innerWidth < 640); paneDrag.current=null; setResizingPane(null);};
    const escape = e => {if (e.key === 'Escape' && !e.defaultPrevented) setSidebar(false);};
    window.addEventListener('resize', resize); window.addEventListener('keydown', escape);
    return () => {window.removeEventListener('resize', resize); window.removeEventListener('keydown', escape);};
  }, []);
  const api = useCallback(async (url, options = {}) => {
    const raw = options.raw; delete options.raw;
    const res = await fetch(url, {...options, headers: {...(raw ? {} : {'Content-Type': 'application/json'}), 'X-CSRF-Token': csrf.current, ...options.headers}});
    const result = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(result.error || result.detail || `Request failed (${res.status}). Try again.`);
    return result;
  }, []);
  const refresh = useCallback(async id => {
    if (!id) return;
    const result = await api(`/api/requests/${encodeURIComponent(id)}`);
    if (selection.current === id) {snapshot.current = result; setData(result);}
    return result;
  }, [api]);
  const choose = useCallback(id => {
    if (busy || (id && id === selection.current)) return;
    setDrafts(prev => ({...prev, [selection.current || 'new']: {text, uploads, references}}));
    setReferences(drafts[id || 'new']?.references || []); setPicker(null);
    setText(drafts[id || 'new']?.text || ''); setUploads(drafts[id || 'new']?.uploads || []); selection.current = id;
    setSelected(id); setMobileView('chat'); setChatShown(true); setData(null); setError(''); setInfo(''); setQIndex(0); setOutputId(null); setSidebar(false);
    snapshot.current = null; setIsNearBottom(true);
    if (id) {location.hash = id; setLoading(true); refresh(id).catch(e => setError(e.message)).finally(() => setLoading(false));}
    else {history.replaceState(null, '', location.pathname); setLoading(false); composer.current?.focus();}
  }, [text, uploads, references, drafts, refresh, busy]);
  useEffect(() => {
    let live = true;
    setFeatures({skills: [], folders: []}); setFeaturesLoading(true); setFeaturesError('');
    api(selected ? `/api/requests/${encodeURIComponent(selected)}/features` : '/api/features')
      .then(result => {if (live) setFeatures(result);})
      .catch(e => {if (live) setFeaturesError(e.message);})
      .finally(() => {if (live) setFeaturesLoading(false);});
    return () => {live = false;};
  }, [selected, api, picker?.kind]);
  useEffect(() => {
    let live = true;
    (async () => {
      const bootstrap = await api('/api/bootstrap'); csrf.current = bootstrap.csrf_token;
      const result = await api('/api/requests'); if (!live) return;
      setRequests(result.requests || []);
      const hash = decodeURIComponent(location.hash.slice(1));
      const id = result.requests?.find(r => r.id === hash)?.id || result.requests?.[0]?.id;
      if (id) {selection.current = id; setSelected(id); await refresh(id);}
      setLoading(false);
    })().catch(e => {if (live) {setError(e.message); setLoading(false);}});
    return () => {live = false;};
  }, [api, refresh]);
  useEffect(() => {
    let live = true, timer;
    const poll = async () => {
      try {
        if (!document.hidden) {
          pollCount.current++;
          if (selected && selection.current === selected) {
            const before = snapshot.current;
            const next = await api(`/api/requests/${encodeURIComponent(selected)}/events?after=${before?.cursor || 0}`);
            if (live && selection.current === selected) {
              const merged = {...before, ...next, events: [...(before?.events || []), ...(next.events || [])]};
              snapshot.current = merged; setData(merged);
              if (before?.active?.id !== next.active?.id || before?.last_job?.id !== next.last_job?.id || pollCount.current % 8 === 0) await refresh(selected);
            }
          }
          if (pollCount.current % 4 === 0) {const result = await api('/api/requests'); if (live) setRequests(result.requests || []);}
          if (live) setError(previous => previous.startsWith('Connection interrupted.') ? '' : previous);
        }
      } catch (e) {if (live) setError(`Connection interrupted. Retrying automatically. ${e.message}`);}
      if (live) timer = setTimeout(poll, 1800);
    };
    timer = setTimeout(poll, 1800);
    return () => {live = false; clearTimeout(timer);};
  }, [selected, api, refresh]);
  useEffect(() => {
    if (!selected || !data || loading) return;
    const result = importedTabs(data.events || [], seenImports.current.get(selected));
    if (!seenImports.current.has(selected)) {try {result.cursor = Math.max(result.cursor, Number(sessionStorage.getItem(`vwb-import-cursor:${selected}`)) || 0);} catch (_) {}}
    seenImports.current.set(selected, result.cursor);
    try {sessionStorage.setItem(`vwb-import-cursor:${selected}`, String(result.cursor));} catch (_) {}
    if (!result.tabs.length) return;
    setCanvases(previous => ({...previous, [selected]: result.tabs.reduce((state, tab) => canvasReducer(state, {type: 'open', tab}), previous[selected] || initialCanvas())}));
    setMobileView('canvas');
  }, [selected, data?.events, loading]);
  useEffect(() => {
    if (!narrow || !sidebar) return;
    const overlay = document.querySelector('.workspace-navigator'), prior = document.activeElement;
    const focusable = () => [...overlay.querySelectorAll('button:not(:disabled),input:not(:disabled),a[href],[tabindex="0"]')];
    focusable()[0]?.focus();
    const trap = e => {
      if (e.key !== 'Tab' || e.defaultPrevented) return;
      const nodes = focusable(), first = nodes[0], last = nodes.at(-1);
      if (e.shiftKey && (document.activeElement === first || !overlay.contains(document.activeElement))) {e.preventDefault(); last?.focus();}
      else if (!e.shiftKey && (document.activeElement === last || !overlay.contains(document.activeElement))) {e.preventDefault(); first?.focus();}
    };
    document.addEventListener('keydown', trap);
    return () => {document.removeEventListener('keydown', trap); prior?.isConnected && prior.focus();};
  }, [narrow, sidebar, navigator]);
  const chat = data?.request?.chat || [], active = data?.active, queue = (data?.queue || []).filter(j => ['queued', 'pending'].includes(j.status));
  const questions = (data?.asks || []).filter(q => ['open', 'defaulted'].includes(q.status));
  const question = questions[Math.min(qIndex, questions.length - 1)];
  const answer = question && !questionsHidden ? answers[question.id] : null;
  const answering = !!question && !questionsHidden;
  const composerText = answering ? questionDrafts[question.id] || '' : text;
  const syncPicker = (value = composerText, caret = composer.current?.selectionStart ?? value.length) => {
    setPicker(answering ? null : triggerAt(value, caret)); setPickIndex(0);
  };
  const setComposerText = value => {
    if (answering) {setQuestionDrafts(prev => ({...prev, [question.id]: value})); setPicker(null);}
    else {setText(value); syncPicker(value);}
  };
  const options = matchingOptions(features, picker, references);
  const pick = option => {
    if (!picker || !option) return;
    if (references.length >= 12) {setError('Choose at most 12 references per message.'); return;}
    const next = composerText.slice(0, picker.start) + composerText.slice(picker.end);
    setText(next); setReferences(prev => prev.some(r => r.kind === option.kind && r.id === option.id) ? prev : [...prev, option]);
    setPicker(null);
    requestAnimationFrame(() => {composer.current?.focus(); composer.current?.setSelectionRange(picker.start, picker.start);});
  };
  const pickerKeys = e => {
    if (!picker || answering || e.target !== composer.current || e.nativeEvent.isComposing) return;
    if (e.key === 'Escape') {e.preventDefault(); e.stopPropagation(); setPicker(null);}
    else if (['ArrowDown', 'ArrowUp'].includes(e.key)) {
      e.preventDefault(); e.stopPropagation();
      setPickIndex(i => options.length ? (i + (e.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length : 0);
    } else if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') {
      if (e.key === 'Tab' && !options.length) {setPicker(null); return;}
      e.preventDefault(); e.stopPropagation(); pick(options[Math.min(pickIndex, options.length - 1)]);
    }
  };
  useEffect(() => {
    const outside = e => {if (!pickerShell.current?.contains(e.target)) setPicker(null);};
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, []);
  useEffect(() => {
    if (answering) setPicker(null);
  }, [answering]);
  useEffect(() => {
    const node = composer.current;
    if (!node) return;
    node.setAttribute('aria-autocomplete', 'list'); node.setAttribute('aria-haspopup', 'listbox');
    node.setAttribute('aria-expanded', String(!!picker && !answering));
    if (picker && !answering) {
      node.setAttribute('aria-controls', 'composer-reference-options');
      if (options.length) node.setAttribute('aria-activedescendant', `reference-option-${Math.min(pickIndex, options.length - 1)}`);
      else node.removeAttribute('aria-activedescendant');
    } else {node.removeAttribute('aria-controls'); node.removeAttribute('aria-activedescendant');}
    document.getElementById(`reference-option-${pickIndex}`)?.scrollIntoView({block: 'nearest'});
  }, [picker, pickIndex, options.length, answering]);
  useEffect(() => {if (qIndex >= questions.length) setQIndex(Math.max(0, questions.length - 1));}, [questions.length, qIndex]);
  useEffect(() => {if (isNearBottom && feed.current) feed.current.scrollTop = feed.current.scrollHeight;}, [chat.length, data?.events?.length, queue.length, selected, loading]);
  const mutate = async (path, body, method = 'POST') => {
    setBusy(true); setError(''); setInfo('');
    try {const result = await api(`/api/requests/${encodeURIComponent(selected)}${path}`, {method, body: JSON.stringify(body || {})}); await refresh(selected); return result;}
    catch (e) {setError(e.message); return null;}
    finally {setBusy(false);}
  };
  const send = async (value = composerText, mode = 'queue') => {
    if (picker && !answering) return;
    if (busy || (!value.trim() && !answer)) return;
    if (!selected) {
      if (!uploads.length) {setError('Attach a source video to start your conversation.'); fileInput.current?.click(); return;}
      setBusy(true); setError('');
      try {
        const result = await api('/api/requests', {method: 'POST', body: JSON.stringify({message: value.trim(), attachments: uploads.map(u => u.id), context: toContext(references)})});
        const id = result.request?.id || result.request_id || (typeof result.request === 'string' ? result.request : result.id);
        selection.current = id; setSelected(id); setText(previous => previous.trim() === value.trim() ? '' : previous); setUploads(previous => previous.filter(item => !uploads.some(sent => sent.id === item.id))); location.hash = id; await refresh(id);
        setReferences(previous => previous.filter(r => !references.some(sent => sent.kind === r.kind && sent.id === r.id)));
      } catch (e) {setError(e.message);} finally {setBusy(false);}
      return;
    }
    const isAnswer = answering && (answer || value.trim());
    const delivery = mode === 'queue' && data?.paused && !active && queue.length === 0 ? 'now' : mode;
    const result = await mutate(isAnswer ? '/answers' : '/messages', isAnswer ? {ticket: question.id, option: answer || null, text: value.trim(), mode: delivery} : {message: value.trim(), mode: delivery, attachments: uploads.map(u => u.id), context: toContext(references)});
    if (result && !isAnswer) setReferences(previous => previous.filter(r => !references.some(sent => sent.kind === r.kind && sent.id === r.id)));
    if (result) {if (isAnswer) setQuestionDrafts(prev => ({...prev, [question.id]: (prev[question.id] || '').trim() === value.trim() ? '' : prev[question.id]})); else {setText(previous => previous.trim() === value.trim() ? '' : previous); setUploads(previous => previous.filter(item => !uploads.some(sent => sent.id === item.id)));} setIsNearBottom(true); if (question) setAnswers(prev => ({...prev, [question.id]: prev[question.id] === answer ? null : prev[question.id]}));}
  };
  const upload = async files => {
    setBusy(true); setError('');
    try {
      for (const file of files) {
        const result = await api('/api/uploads', {method: 'POST', raw: true, headers: {'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)}, body: file});
        setUploads(prev => [...prev, {id: result.id || result.upload?.id, name: file.name}]);
      }
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  };
  const compact = async () => {
    setCompactBusy(true); const result = await mutate('/compact'); setCompactBusy(false);
    if (result) setInfo('Compaction requested. Your conversation and files are kept.');
  };
  const current = requests.find(r => r.id === selected);
  const name = selected ? title(data?.request?.sources?.[0]?.name || current?.title || 'Conversation') : 'New conversation';
  const failure = !active && data?.last_job?.status === 'failed' ? (data.last_job.error || 'The last turn failed. Send a message to try again.') : '';
  const notice = error || failure ? {tone: 'error', text: html`<span>${error || failure}${data?.paused && queue.length > 0 && html` <button className="text-action" onClick=${() => mutate('/resume')}>Resume queued messages</button>`}</span>`} : data?.paused && queue.length ? {tone: 'info', text: html`<span>Queue paused. <button className="text-action" onClick=${() => mutate('/resume')}>Resume ${queue.length} queued ${queue.length === 1 ? 'message' : 'messages'}</button></span>`} : info ? {tone: 'info', text: info} : active ? {tone: 'progress', text: active.kind === 'compact' ? 'Compacting your conversation…' : 'The agent is working. You can keep writing.'} : questions.length ? {tone: 'info', text: `${questions.length} ${questions.length === 1 ? 'choice is' : 'choices are'} ready for your input.`} : null;
  const messageEvents = (start, end) => (data?.events || []).filter(e => stamp(e.ts) >= stamp(start) && (!end || stamp(e.ts) <= stamp(end)));
  const latestHuman = [...chat].reverse().find(m => m.role === 'human');
  const rows = requests.filter(r => `${r.title} ${r.id}`.toLowerCase().includes(query.toLowerCase()));
  // Eeva Surface does not forward style, so pane variables belong on a native element.
  return html`<${Surface} theme="editorial" background="chat">
    <div className=${`workbench canvas-workbench ${navigatorOpen ? 'navigator-open' : ''} ${sidebar ? 'nav-open' : ''} ${chatShown ? 'chat-shown' : 'chat-hidden'} mobile-view-${mobileView}`} style=${{'--chat-width': `${layout.chat}px`, '--navigator-width': `${layout.navigator}px`}}>
    ${resizingPane && html`<div className="pane-resize-shield" aria-hidden="true"></div>`}
    <a href="#conversation-feed" className="skip-link">Skip to conversation</a>
    <nav className="activity-rail" aria-label="Workbench sections"><div className="rail-brand" aria-label="Solstice video workbench"><${SolsticeMark} size=${24}/></div>
      <${Tooltip} content="Conversations"><button aria-label="Conversations" aria-pressed=${navigator === 'conversations' && (narrow ? sidebar : navigatorOpen)} onClick=${() => toggleNavigator('conversations')}><${Icon} name="library" size=${18}/></button><//>
      <${Tooltip} content="Workspace files"><button aria-label="Workspace files" aria-pressed=${navigator === 'workspace' && (narrow ? sidebar : navigatorOpen)} onClick=${() => toggleNavigator('workspace')}><${Icon} name="folder" size=${18}/></button><//>
      <div className="rail-bottom"><${Tooltip} content="Local workbench"><span tabIndex="0" aria-label="Local workbench"><span className="local-dot"></span></span><//></div>
    </nav>
    ${narrow && sidebar && html`<button className="nav-scrim" aria-label="Close navigator" onClick=${() => setSidebar(false)}></button>`}
    <nav className="sidebar workspace-navigator" aria-label=${navigator === 'workspace' ? 'Workspace navigator' : 'Conversations navigator'} inert=${(narrow ? !sidebar : !navigatorOpen) ? '' : undefined}>
      ${navigator === 'conversations' ? html`<div className="navigator-heading"><h2>Conversations</h2><${IconButton} name="panel-right-close" label="Collapse navigator" onClick=${() => narrow ? setSidebar(false) : setNavigatorOpen(false)}/></div>
      <${Button} variant="secondary" size="sm" className="new-chat" onClick=${() => choose(null)}><${Icon} name="plus" size=${14}/> New conversation<//>
      <label className="conversation-search"><${Icon} name="search" size=${14}/><input value=${query} onChange=${e => setQuery(e.target.value)} placeholder="Find a conversation" aria-label="Find a conversation"/></label>
      <div className="section-label conversation-label">Conversations</div>
      <div className="conversation-list">${rows.map(r => html`<button key=${r.id} className=${`conversation-row ${selected === r.id ? 'current' : ''}`} onClick=${() => choose(r.id)} aria-current=${selected === r.id ? 'page' : undefined}>
        <span className="conversation-name">${title(r.title)}</span><span className="conversation-meta">${date(r.created)}<span>${r.active ? 'Working' : r.output_count ? `${r.output_count} outputs` : 'Conversation'}</span></span>
      </button>`)}${!rows.length && html`<p className="caption search-empty">${query ? 'No conversations match.' : 'Your conversations will appear here.'}</p>`}</div>
      <div className="sidebar-footer"><span className="local-dot"></span><span>Local workspace</span><${Tooltip} content="Your videos and conversation stay in this workbench’s local files."><span tabIndex="0" aria-label="Local workspace information"><${Icon} name="info" size=${13}/></span><//></div>

      ` : html`<${WorkspaceExplorer} api=${api} requestId=${selected} activePath=${activeCanvasPath} onOpen=${openCanvas} onOutputs=${() => openCanvas({id:'outputs',kind:'outputs',title:'Outputs'})} onBrowser=${() => openCanvas({id:'browser',kind:'browser',title:'Browser'})}/>`}
    </nav>
    ${divider('navigator')}
    <main className="main-workspace" inert=${narrow && sidebar ? '' : undefined}>
      <header className="workspace-header"><div className="header-leading"><div><div className="header-path">Video workbench <span>/</span> ${selected ? 'Conversation' : 'Get started'}</div><h1>${name}</h1></div></div>
        <div className="header-actions workspace-view-actions" role="group" aria-label="Workspace view">
          <${Button} variant="text" size="sm" aria-label=${narrow ? 'Show chat' : chatShown ? 'Hide chat' : 'Show chat'} aria-pressed=${narrow ? mobileView === 'chat' : chatShown} onClick=${() => {if (narrow) setMobileView('chat'); else setChatShown(value => !value);}}><${Icon} name="file-text" size=${16}/><span>Chat</span><//>
          ${narrow && html`<${Button} variant="text" size="sm" aria-label="Show canvas" aria-pressed=${mobileView === 'canvas'} onClick=${() => setMobileView('canvas')}><${Icon} name="table" size=${16}/><span>Canvas</span><//>`}
          <${Tooltip} content="Open outputs"><${Button} variant="text" size="sm" aria-label="Show outputs and request" onClick=${() => openCanvas({id:'outputs',kind:'outputs',title:'Outputs'})}><${Icon} name="file" size=${16}/><span className="panel-button-label">Outputs</span><//><//>
          <${Tooltip} content="Open browser"><${Button} variant="text" size="sm" aria-label="Show browser" onClick=${() => openCanvas({id:'browser',kind:'browser',title:'Browser'})}><${Icon} name="link" size=${16}/><span className="panel-button-label">Browser</span><//><//></div>
      </header>
      <div className="workspace-body"><section className="chat-column" aria-label="Agent conversation" inert=${(!chatShown && !narrow) || (narrow && mobileView !== 'chat') ? '' : undefined}>
        <div className="chat-pane-heading"><h2>Conversation</h2><span>${active ? 'Working' : current?.output_count ? `${current.output_count} outputs` : 'Ready'}</span><${IconButton} name="chevron-left" label="Collapse chat" onClick=${() => narrow ? setMobileView('canvas') : setChatShown(false)}/></div>
        ${notice && html`<div className="chat-panel-status"><${MessageBar} tone=${notice.tone}>${notice.text}<//></div>`}
        <div id="conversation-feed" className="conversation-feed" ref=${feed} tabIndex="0" onScroll=${e => setIsNearBottom(e.currentTarget.scrollHeight - e.currentTarget.scrollTop - e.currentTarget.clientHeight < 100)}>
          <div className="feed-inner">${loading ? html`<p className="loading-copy">Opening your conversation…</p>` : !chat.length ? html`<div className="welcome"><${SolsticeMark} size=${28}/><h2>Good work starts<br/>with a conversation.</h2><p>Bring a video. Tell me what you want to make.<br/>We’ll shape it together, one edit at a time.</p><div className="welcome-hint"><${Icon} name="file" size=${16}/><span>Attach your source video to get started</span></div></div>` : html`
            <div className="conversation-start"><span>${date(data.request.created)}</span><span>One continuing conversation</span></div>
            ${chat.map((m, i) => m.role === 'human' ? html`<div key=${`chat-${i}`}><${ReferenceTags} context=${m.context}/><${UserMessage} time=${time(m.ts)}>${m.text}<//>${((chat[i + 1] && chat[i + 1].role === 'human') || (!chat[i + 1] && !active)) && html`<${Activity} events=${messageEvents(m.ts, chat[i + 1]?.ts)} state=${data?.last_job?.status === 'failed' ? 'failed' : 'stopped'}/>`}</div>` : html`<div className="agent-turn" key=${`chat-${i}`}>
              <${Activity} events=${messageEvents(chat[i - 1]?.ts, m.ts)}/><${AgentMessage} blocks=${blocks(m.text)} time=${time(m.ts)}/></div>`)}
            ${active && html`<${Activity} events=${messageEvents(latestHuman?.ts)} working started=${active.started_at || active.created_at}/>`}
            ${queue.map(j => html`<div key=${j.id}><${ReferenceTags} context=${j.context}/><${UserMessage} queued onRemoveQueued=${() => mutate(`/queue/${j.id}`, {}, 'DELETE')}>${j.message || j.text}<//></div>`)}
          `}</div>
        </div>
        ${!isNearBottom && html`<div className="jump-row"><${Button} variant="secondary" size="sm" onClick=${() => {feed.current.scrollTo({top: feed.current.scrollHeight, behavior: 'smooth'}); setIsNearBottom(true);}}>Jump to latest <${Icon} name="chevron-down" size=${14}/><//></div>`}
        <div className="composer-dock"><div className="composer-width"><div className="composer-shell" ref=${pickerShell} onKeyDownCapture=${pickerKeys}
          onKeyUp=${e => {if (e.target === composer.current && !['Escape', 'Enter', 'Tab', 'ArrowUp', 'ArrowDown'].includes(e.key)) syncPicker();}}
          onClick=${e => {if (e.target === composer.current) syncPicker();}}>
          ${picker && !answering && html`<div className="reference-dropup" aria-label=${picker.kind === 'skill' ? 'Available skills' : 'Workspace folders'}>
            <div className="reference-heading"><span>${picker.kind === 'skill' ? 'Features · Skills' : 'Workspace folders'}</span><span className="caption">↑ ↓ to choose · Enter to add</span></div>
            <div id="composer-reference-options" role="listbox" aria-label=${picker.kind === 'skill' ? 'Choose a skill' : 'Choose a folder'}>
              ${options.map((option, i) => html`<button key=${`${option.kind}:${option.id}`} id=${`reference-option-${i}`} role="option" tabIndex="-1" aria-selected=${i === Math.min(pickIndex, options.length - 1)}
                className=${`reference-option ${i === Math.min(pickIndex, options.length - 1) ? 'highlighted' : ''}`} onMouseDown=${e => e.preventDefault()} onClick=${() => pick(option)}>
                <${Icon} name=${option.kind === 'skill' ? 'sparkles' : 'folder'} size=${16}/><span><strong>${option.name}</strong><span>${option.description}</span></span><//>`)}
              ${!options.length && html`<p className="reference-empty" role="status">${featuresLoading ? 'Loading available references…' : featuresError || (picker.kind === 'folder' && !selected ? 'Choose a conversation to see its folders.' : 'No matching references. Try another search.')}</p>`}
            </div><div className="reference-footer">${picker.kind === 'skill' ? 'Choose a skill, then describe what you want to do.' : 'Folders are indexed from this conversation’s work/ workspace.'}${features.folders_truncated && picker.kind === 'folder' ? ' Showing the first 500 folders.' : ''}</div>
          </div>`}
          ${!answering && references.length > 0 && html`<div className="reference-chips" role="group" aria-label="Selected references">${references.map(r => html`<span className="reference-chip" key=${`${r.kind}:${r.id}`}>
            <${Icon} name=${r.kind === 'skill' ? 'sparkles' : r.kind === 'plugin' ? 'library' : 'folder'} size=${13}/><span>${r.kind === 'skill' ? `/features ${r.name}` : r.kind === 'plugin' ? r.name : `@${r.name}`}</span>
            <${IconButton} name="x" label=${`Remove ${r.name}`} onClick=${() => setReferences(prev => prev.filter(x => x.kind !== r.kind || x.id !== r.id))}/></span>`)}</div>`}
          <${Composer} ref=${composer} value=${composerText} onValueChange=${setComposerText} onSend=${value => send(value)} working=${!!active}
            canSend=${!busy && (!picker || answering) && (!!composerText.trim() || !!answer)} onStop=${active?.can_stop === false ? undefined : () => mutate('/stop')} onAttach=${answering || busy ? undefined : () => fileInput.current?.click()}
            label=${answering ? 'Your answer to the selected question' : 'Message the agent'}
            placeholder=${question && !questionsHidden ? 'Choose an option, or write your answer…' : active ? 'Add a message to the queue…' : 'What would you like to change?'}
            question=${questions.length ? html`<${QuestionQueue} questions=${questions} index=${qIndex} setIndex=${setQIndex} answer=${answer} setAnswer=${value => setAnswers(prev => ({...prev, [question.id]: value}))} hidden=${questionsHidden} setHidden=${setQuestionsHidden}/>` : undefined}
            attachments=${uploads.length ? html`<div className="staged-files">${uploads.map(u => html`<span className="staged-file" key=${u.id}><${Icon} name="file" size=${14}/>${u.name}<${IconButton} name="x" label=${`Remove ${u.name}`} onClick=${() => setUploads(prev => prev.filter(x => x.id !== u.id))}/></span>`)}</div>` : answer ? html`<span className="selected-answer"><${Icon} name="check" size=${12}/> Answer selected. Send to confirm.</span>` : undefined}
            className="workbench-composer"/>
          <div className="composer-footer"><div className="composer-model"><${ModelControl} api=${api} requestId=${selected} onNotice=${notice => {if (notice.tone === 'error') setError(notice.text); else setInfo(notice.text);}}/><${IntegrationsControl} api=${api} disabled=${answering || busy} selected=${references.filter(r => r.kind === 'plugin')} onToggle=${item => {
              if (!references.some(r => r.kind === 'plugin' && r.id === item.id) && references.length >= 12) {setError('Choose at most 12 references per message.'); return;}
              setReferences(previous => previous.some(r => r.kind === 'plugin' && r.id === item.id) ? previous.filter(r => r.kind !== 'plugin' || r.id !== item.id) : [...previous,item]);
            }}/>${active && html`<span className="queue-label">Send queues</span>`}</div>
            <div className="composer-footer-actions">${active && active.can_stop !== false && html`<button className="send-now" disabled=${busy || (picker && !answering) || (!composerText.trim() && !answer)} onClick=${() => send(composerText, 'now')}>Send now <${Icon} name="arrow-up" size=${12}/></button>`}
              <${ContextUsage} usage=${data?.usage} active=${active} pending=${queue.length > 0} compact=${compact} busy=${compactBusy}/></div></div>
        </div><div className="composer-help"><span>${busy ? 'Saving…' : answering ? (uploads.length ? 'Your attached videos are kept for your next general message.' : 'Answering this question. Collapse it to write another message.') : active ? (active.can_stop === false ? 'This turn is running in a terminal. Your message will queue.' : 'Send now stops the current turn and continues with your message.') : 'Enter to send · Shift + Enter for a new line'}</span><span>${selected ? 'Changes stay in this conversation' : 'MP4, MOV, MKV or WebM'}</span></div></div></div>
        <input ref=${fileInput} className="visually-hidden" type="file" multiple accept="video/*,.mkv" onChange=${e => {upload([...e.target.files]); e.target.value = '';}} aria-label="Attach source videos"/>
      </section>
      ${divider('chat')}
      <${WorkspaceCanvas} visible=${!narrow || mobileView === 'canvas'} api=${api} requestId=${selected} state=${canvas} dispatch=${dispatchCanvas} onExplore=${explore}
        renderOutputs=${() => html`<${OutputPanel} data=${data} outputId=${outputId} setOutputId=${setOutputId} onClose=${() => dispatchCanvas({type:'close',id:'outputs'})}/>`}/>

      </div>
    </main>
    </div>
  <//>`;
}

class ErrorBoundary extends React.Component {
  constructor(props) {super(props); this.state = {error: null};}
  static getDerivedStateFromError(error) {return {error};}
  render() {return this.state.error ? html`<main className="boot"><h1>The workbench couldn’t open</h1><p>${this.state.error.message}</p><button onClick=${() => location.reload()}>Reload workbench</button></main>` : this.props.children;}
}
ReactDOM.createRoot(document.getElementById('root')).render(html`<${ErrorBoundary}><${App}/><//>`);
