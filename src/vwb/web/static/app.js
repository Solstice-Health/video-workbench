import htm from './vendor/htm.mjs';
import {triggerAt, matchingOptions, toContext, workspaceReference, displayPath} from './composer-picker.js';
import {ModelControl} from './model-control.js';
import {WorkspaceExplorer, WorkspaceCanvas, initialCanvas, canvasReducer, importedTabs, studioTab, fileTab, formatBytes} from './workspace-canvas.js';
import {IntegrationsControl} from './integrations-control.js';
import {COMPACT_WIDTH, paneLayout, panePreferences} from './pane-layout.js';
import {LocalImportDialog, localPath} from './local-import-control.js';
import {QCPanel} from './qc-panel.js';
import {EditorOutputPreview, editorMediaKey} from './editor-qc-markers.js';
import {ProjectNavigation, MessageActions, HistoryDialog} from './conversation-controls.js';

// Eeva and React are local, versioned assets. No network service is needed to open the UI.
const {createElement, useState, useEffect, useRef, useCallback, useMemo} = window.React;
const memo = window.React.memo || (component => component);
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

export function turnRecovery(snapshot) {
  if (!snapshot || snapshot.active) return {job: null, queueCount: 0};
  const candidate = snapshot.resumable_job;
  const interrupted = ['failed', 'stopped'].includes(candidate?.status) || (candidate?.status === 'cancelled' && !!candidate.resume_of);
  const job = candidate?.id && ['message', 'compact'].includes(candidate.kind) && interrupted ? candidate : null;
  const queueCount = snapshot.paused ? (snapshot.queue || []).filter(job => ['queued', 'pending'].includes(job.status)).length : 0;
  return {job, queueCount};
}

export function RecoveryActions({recovery, busy, onResume}) {
  return html`<span className="recovery-actions">
    ${recovery.job && html`<button className="text-action" disabled=${busy} onClick=${() => onResume({job_id: recovery.job.id, use_current_model: true, use_current_prompt: true})}
      title="Continue using the selected model and prompt">Resume turn</button>`}
    ${recovery.queueCount > 0 && html`<button className="text-action" disabled=${busy} onClick=${() => onResume({})}>Resume queued messages</button>`}
  </span>`;
}

function IconButton({name, label, onClick, disabled, ...rest}) {
  return html`<${Tooltip} content=${label}><${Button} variant="text" iconOnly aria-label=${label}
    onClick=${onClick} disabled=${disabled} ...${rest}><${Icon} name=${name} size=${16}/><//><//>`;
}

function ReferenceTags({context}) {
  const pathLabel = path => ({label: `@${displayPath(path)}`, full: `@${path}`});
  const labels = [...(context?.skills || []).map(id => ({label: `/features ${id}`, full: `/features ${id}`})),
    ...(context?.folders || []).map(id => pathLabel(`work/${id === '.' ? '' : `${id}/`}`)),
    ...(context?.files || []).map(id => pathLabel(`work/${id}`)),
    ...(context?.plugins || []).map(id => ({label: `Plugin: ${id}`, full: `Plugin: ${id}`}))];
  return labels.length ? html`<div className="message-references" aria-label="Message references">${labels.map(item => html`<${Tooltip} key=${item.full} content=${item.full}><span title=${item.full} aria-label=${item.full}>${item.label}</span><//>`)}</div>` : null;
}

function ChatAttachments({items = [], onOpen}) {
  return items.length ? html`<div className="chat-attachments" aria-label="Attached files">${items.map(item => html`<div className="chat-attachment" key=${item.id}>
    <button className="attachment-open" onClick=${() => onOpen(fileTab({path: item.workspace_path || `work/${item.path}`, name: item.name}))} aria-label=${`Open ${item.name} in canvas`}>
      ${item.kind === 'image' && item.url ? html`<img src=${item.url} alt="" loading="lazy"/>` : html`<${Icon} name="file" size=${18}/>`}
      <span><strong>${item.name}</strong><small>${formatBytes(item.bytes)} · ${item.kind === 'download' ? 'File' : item.kind}</small></span>
    </button><a className="attachment-download" href=${item.url} download=${item.name} aria-label=${`Download original ${item.name}`}>Original</a>
  </div>`)}</div>` : null;
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

export function coalesceEvents(...batches) {
  const map = new Map();
  for (const events of batches) for (const event of events || []) {
    const key = event.data?.id ? JSON.stringify([event.kind, event.actor || '', event.data.id]) : `seq-${event.seq}`;
    const previous = map.get(key);
    if (!previous || Number(event.seq) > Number(previous.seq)) map.set(key, event);
  }
  return [...map.values()].sort((a, b) => a.seq - b.seq);
}

export function mergeEventPage(before, page, {older = false, snapshot = false} = {}) {
  const epoch = page.history_epoch ?? before?.history_epoch ?? 0;
  if (before && epoch < (before.history_epoch ?? 0)) return before;
  if (before && epoch > (before.history_epoch ?? 0)) {
    // Event sequence numbers restart after a history change. A reset response
    // is a fresh snapshot even when the request asked for an older page.
    before = null; older = false; snapshot = true;
  }
  const events = page.events?.length ? coalesceEvents(before?.events, page.events) : before?.events || [];
  if (older) return {...before, events, events_before: page.events_before,
    events_has_more: !!page.events_has_more};
  const merged = {...before, ...page, events};
  if (before && !snapshot) {
    merged.events_before = before.events_before;
    merged.events_has_more = before.events_has_more;
  } else if (before?.events_before && before.events_before < (page.events_before || Infinity)) {
    merged.events_before = before.events_before;
    merged.events_has_more = before.events_has_more;
  }
  merged.cursor = Math.max(Number(before?.cursor) || 0, Number(page.cursor) || 0);
  return merged;
}

export function activityIndex(events) {
  const rows = (events || []).filter(e => ['thinking', 'tool', 'message'].includes(e.kind) || e.kind?.startsWith('agent.'))
    .map(event => ({event, at: stamp(event.ts)})).sort((a, b) => a.at - b.at || a.event.seq - b.event.seq);
  const cached = new Map();
  const bound = (at, upper) => {
    let low = 0, high = rows.length;
    while (low < high) {const mid = (low + high) >>> 1; if (rows[mid].at < at || (upper && rows[mid].at === at)) low = mid + 1; else high = mid;}
    return low;
  };
  return (start, end) => {
    const from = stamp(start), to = end ? stamp(end) : Infinity, key = `${from}:${to}`;
    if (!cached.has(key)) cached.set(key, rows.slice(bound(from, false), bound(to, true)).map(row => row.event));
    return cached.get(key);
  };
}

export const ToolCall = memo(function ToolCall({event, settledState}) {
  const [expanded, setExpanded] = useState(false);
  const d = event.data || {}, done = d.status === 'completed', failed = d.status === 'failed' || (!done && settledState === 'failed');
  const stopped = !done && !failed && settledState === 'stopped';
  const command = d.arguments?.command || d.arguments?.cmd || d.command;
  const label = d.name || (command ? 'Bash' : 'Tool');
  const preview = String(command || d.text || (d.arguments ? plain(d.arguments) : '')).replace(/\s+/g, ' ').slice(0, 160);
  return html`<details className="tool-call" open=${expanded} onToggle=${event => {if (event.target === event.currentTarget) setExpanded(event.currentTarget.open);}}>
    <summary><${Icon} name=${failed ? 'circle-alert' : done ? 'check' : 'square'} size=${13}/>
      <span className="tool-name">${label.replace(/^mcp__vwb__/, '')}</span>
      <span className="tool-preview">${preview}</span>
      <span className="tool-state">${failed ? 'Failed' : done ? 'Done' : stopped ? 'Stopped' : 'Running'}</span><${Icon} name="chevron-down" size=${13}/>
    </summary>
    ${expanded && html`<div className="tool-detail">${d.arguments && html`<div className="section-label">Input</div><pre>${plain(d.arguments)}</pre>`}
      ${(d.output || d.text) && html`<div className="section-label">Output</div><pre>${d.output || d.text}</pre>`}
      ${d.exit_code != null && html`<p className="caption">Exit code ${d.exit_code}</p>`}</div>`}
  </details>`;
});

export const Activity = memo(function Activity({events, working = false, started, state = 'done'}) {
  const [expanded, setExpanded] = useState(false), [allTools, setAllTools] = useState(false);
  const visible = events || [];
  const thoughts = visible.filter(e => e.kind === 'thinking' && e.data?.text);
  const tools = visible.filter(e => e.kind === 'tool');
  const messages = visible.filter(e => e.kind === 'message' && e.data?.text);
  const agentEvents = visible.filter(e => e.kind.startsWith('agent.'));
  if (!thoughts.length && !tools.length && !working && !messages.length && !agentEvents.length) return null;
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
    <${AgentActivity} events=${agentEvents} working=${working}/>
    ${working && messages.at(-1) && html`<${AgentMessage} blocks=${blocks(messages.at(-1).data.text)}/>`}
  </div>`;
}, (before, after) => before.working === after.working && before.started === after.started && before.state === after.state
  && before.events?.length === after.events?.length && (before.events || []).every((event, index) => event === after.events[index]));

function AgentToolDetail({tool}) {
  const [expanded, setExpanded] = useState(false);
  return html`<details className="agent-tool-detail" open=${expanded} onToggle=${event => {if (event.target === event.currentTarget) setExpanded(event.currentTarget.open);}}>
    <summary>${tool.tool} · ${tool.status}</summary>${expanded && html`<pre>${tool.output}</pre>`}</details>`;
}

function AgentActivity({events, working}) {
  const [expanded, setExpanded] = useState(false);
  const agents = new Map();
  for (const event of events) {
    const d = event.data || {};
    let id = d.ticket || (event.kind === 'agent.result' ? event.actor : null);
    if (!id || id === 'job_lead') continue;
    const row = agents.get(id) || {id, status: 'running', history: []};
    if (event.kind === 'agent.assign') Object.assign(row, {parent: d.parent, role: d.role, task: d.gist, depth: d.depth});
    if (event.kind === 'agent.activity') Object.assign(row, {tool: d.tool, calls: d.tool_calls});
    if (event.kind === 'agent.progress') row.progress = d.text;
    if (event.kind === 'agent.followup') row.status = 'running';
    if (event.kind === 'agent.interrupt' || event.kind === 'agent.cancel') row.status = 'stopping';
    if (event.kind === 'agent.result') {
      try {const result = JSON.parse(d.text); row.status = result.status; row.result = plain(result.result);} catch (_) {row.status = d.status || 'done';}
    }
    if (event.kind === 'agent.tool') row.history.push(d);
    agents.set(id, row);
  }
  if (!agents.size) return null;
  return html`<details className="agent-monitor" open=${expanded} onToggle=${event => {if (event.target === event.currentTarget) setExpanded(event.currentTarget.open);}}><summary>${agents.size} ${agents.size === 1 ? 'helper' : 'helpers'} · task and activity</summary>
    ${expanded && [...agents.values()].map(row => html`<details key=${row.id} className="agent-monitor-row"><summary><span>${row.role || row.id}</span><span className="caption">${!working && ['running','stopping'].includes(row.status) ? 'Stopped' : row.status} ${row.calls ? `· ${row.calls} tool calls` : ''}</span></summary>
      <p>${row.task || row.id}</p>${row.parent && html`<p className="caption">Parent: ${row.parent}</p>`}
      ${row.tool && html`<p className="caption">Latest tool: ${row.tool}</p>`}
      ${row.progress && html`<p>${row.progress}</p>`}
      ${row.history.slice(-5).map((tool,i) => html`<${AgentToolDetail} key=${i} tool=${tool}/>`)}
      ${row.result && html`<p>${row.result}</p>`}
    </details>`)}
  </details>`;
}

export function ContextUsage({usage, active, pending, compact, busy}) {
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
      ${ctx.max_output_tokens != null && html`<dl className="usage-rows"><div><dt>Output limit per call</dt><dd>${number(ctx.max_output_tokens)}</dd></div></dl><p className="caption">Includes reasoning when the provider counts it toward output. This limit is separate from the context window.</p>`}
      ${ctx.auto_compaction_enabled && html`<dl className="usage-rows"><div><dt>Automatic compactions</dt><dd>${number(ctx.compaction_count)}</dd></div></dl>`}
      <div className="usage-billing"><h4>Conversation totals</h4><dl className="usage-rows">
        <div><dt>Input tokens</dt><dd>${number(bill.input_tokens)}</dd></div>
        <div><dt>Output tokens</dt><dd>${number(bill.output_tokens)}</dd></div>
        <div><dt>Model calls</dt><dd>${number(bill.calls)}</dd></div></dl>
        <p className="caption">Cumulative usage, including helpers. This is separate from the current context.</p></div>
      <${Button} variant="secondary" size="sm" className="compact-button" disabled=${!!active || pending || busy || !usage?.compact_available} onClick=${compact}>
        <${Icon} name="refresh" size=${14}/> ${busy ? 'Compacting…' : 'Compact conversation'}<//>
      <p className="caption">${ctx.auto_compaction_enabled ? `Automatic compaction runs at ${number(ctx.auto_compact_tokens)} input tokens. Files and full history remain saved.` : active || pending ? 'Available after the current turn and queued messages finish.' : usage?.compact_available ? 'Keeps your thread, decisions and files. Earlier context is summarized.' : 'Manual compaction is available for Codex conversations.'}</p>
    </section>`}
  </div>`;
}

export function QuestionQueue({questions, index, setIndex, answer, setAnswer, hidden, setHidden, answering, onMode}) {
  if (!questions.length) return null;
  const q = questions[Math.min(index, questions.length - 1)];
  return html`<section className="question-queue" aria-label="Questions from the agent">
    <div className="question-heading"><button className="question-toggle" onClick=${() => setHidden(!hidden)} aria-expanded=${!hidden}>
      <${Icon} name="circle-alert" size=${14}/><span>${q.status === 'defaulted' ? 'Review an earlier choice' : questionWaiting(q) ? 'Agent waiting for you' : 'Your input'}</span><${Icon} name=${hidden ? 'chevron-right' : 'chevron-down'} size=${13}/></button>
      <div className="question-navigation"><span className="caption">${index + 1} of ${questions.length}</span>
        <${IconButton} name="chevron-left" label="Previous question" disabled=${index === 0} onClick=${() => setIndex(index - 1)}/>
        <${IconButton} name="chevron-right" label="Next question" disabled=${index >= questions.length - 1} onClick=${() => setIndex(index + 1)}/></div></div>
    ${!hidden && html`<h3>${q.question}</h3>${q.why && html`<p className="question-why">${q.why}</p>`}
      <div role="group" aria-label="Composer purpose" className="question-options">
        <button className=${`question-option ${answering ? 'selected' : ''}`} aria-pressed=${answering} onClick=${() => onMode('answer')}>Answer question</button>
        <button className=${`question-option ${!answering ? 'selected' : ''}`} aria-pressed=${!answering} onClick=${() => onMode('message')}>New instruction</button>
      </div>
      ${q.status === 'defaulted' && html`<p className="caption">The agent used its recommended option while waiting. You can change it.</p>`}
      ${questionWaiting(q) && html`<p className="caption">Your answer resumes the waiting agent. Other helpers can keep working.</p>`}
      <div role="group" aria-label=${q.question} className="question-options">
        ${(q.options || []).map((o, i) => html`<button key=${o.id} aria-pressed=${answer === o.id} className=${`question-option ${answer === o.id ? 'selected' : ''}`} onClick=${() => setAnswer(answer === o.id ? null : o.id)}>
          <span className="option-key">${i + 1}</span><span className="option-content"><span>${o.label || o.text || o.id}</span>
          ${o.description && html`<span className="caption">${o.description}</span>`}</span>${q.recommended === o.id && html`<span className="recommended">Recommended</span>`}
          ${answer === o.id && html`<${Icon} name="check" size=${14}/>`}</button>`)}
      </div>`}
  </section>`;
}

export function questionWaiting(q) {
  return q?.status === 'open' && Object.values(q.waiters || {}).some(w => w.expires_at * 1000 > Date.now());
}

export function SteeringAction({active, answering, waiting, disabled, onSend}) {
  if (!(answering && waiting) && (!active || active.can_stop === false)) return null;
  return html`<button className="send-now" disabled=${disabled} onClick=${onSend}
    title=${answering && waiting ? 'Resume the waiting agent with this answer' : `Stop the current turn and continue with your ${answering ? 'answer' : 'instruction'}`}>
    ${answering ? 'Answer now' : 'Send now'} <${Icon} name="arrow-up" size=${12}/></button>`;
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
          <${EditorOutputPreview} key=${`${req?.id}:${editorMediaKey(output)}`} output=${output} findings=${data?.qc_findings || []}/>
          <div className="output-metadata"><span>${output.width || '—'} × ${output.height || '—'}${output.duration_s ? ` · ${output.duration_s.toFixed(1)}s` : ''}</span><span>Version ${output.version}</span></div>
          <div className="review-heading"><h3>Review</h3><span className=${`status-pill ${output.triage === 'GREEN' ? 'success' : 'required'}`}><${Icon} name=${output.triage === 'GREEN' ? 'check' : 'circle-alert'} size=${12}/>${output.triage === 'GREEN' ? 'Ready' : 'Needs review'}</span></div>
          <p className="review-explanation">${output.triage === 'GREEN' ? 'Checks are complete for this version.' : 'Check the flagged details before accepting this version.'}</p>
          <div className="review-flags">${(output.flags || []).slice(0, 4).map((f, i) => html`<details key=${f.id || i}><summary><${Icon} name="chevron-right" size=${13}/><span>${(f.kind || f.check || 'Review detail').replace(/[-_.]/g, ' ').toLowerCase()}</span></summary><p>${f.summary || f.message}</p></details>`)}</div>
          <div className="output-links">${output.review_url && html`<a className="secondary-link" href=${output.review_url} target="_blank" rel="noreferrer">Open full review <${Icon} name="arrow-up" size=${14}/></a>`}
            ${output.video_url && html`<a className="text-link" href=${output.video_url} download>Download video</a>`}</div>` : html`<div className="panel-empty"><${Icon} name="file" size=${18}/><h3>Your videos appear here</h3><p>As the agent works, you can preview each output and review its changes.</p></div>`}
      <//>
      <${TabPanel} value="details" className="request-content"><h3>What we’re making</h3><p>${req?.spec?.summary || req?.message || 'Describe the video you want to make, share a design link, or attach a source video.'}</p>
        ${req?.spec?.constraints?.length > 0 && html`<h3>Constraints</h3><ul>${req.spec.constraints.map(c => html`<li key=${c}>${c}</li>`)}</ul>`}
        <h3>Source files</h3>${req?.sources?.length ? req.sources.map(s => html`<div className="source-file" key=${s.media_id}><${Icon} name="file" size=${16}/><span>${s.name || s.original_path?.split('/').at(-1) || s.media_id}</span></div>`) : html`<p>No source attached yet.</p>`}
      <//>
    <//>
  </aside>`;
}

function App() {
  const [requests, setRequests] = useState([]), [selected, setSelected] = useState(null), [data, setData] = useState(null);
  const [projects, setProjects] = useState([]), [historyAction, setHistoryAction] = useState(null), [historyError, setHistoryError] = useState('');
  const projectKeys = useRef(new Map()), historyKeys = useRef(new Map());
  const [text, setText] = useState(''), [drafts, setDrafts] = useState({}), [uploads, setUploads] = useState([]);
  const [references, setReferences] = useState([]), [features, setFeatures] = useState({skills: [], folders: [], files: []});
  const [picker, setPicker] = useState(null), [pickIndex, setPickIndex] = useState(0), [featuresLoading, setFeaturesLoading] = useState(false);
  const [featuresError, setFeaturesError] = useState('');
  const [localImport, setLocalImport] = useState(null), [importPending, setImportPending] = useState(false), [featuresVersion, setFeaturesVersion] = useState(0);
  const localImportRef = useRef(localImport); localImportRef.current = localImport;
  const pickerShell = useRef(null);
  const [error, setError] = useState(''), [info, setInfo] = useState(''), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [sidebar, setSidebar] = useState(false), [outputId, setOutputId] = useState(null);
  const [navigator, setNavigator] = useState('conversations'), [navigatorOpen, setNavigatorOpen] = useState(() => window.innerWidth >= 1024);
  const [chatShown, setChatShown] = useState(true), [mobileView, setMobileView] = useState('chat');
  const [chatTab, setChatTab] = useState('edit');
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
  const [questionModes, setQuestionModes] = useState({});
  const [query, setQuery] = useState(''), [compactBusy, setCompactBusy] = useState(false), [isNearBottom, setIsNearBottom] = useState(true);
  const [historyBusy, setHistoryBusy] = useState(false), [visibleMessages, setVisibleMessages] = useState(40);
  const csrf = useRef(''), selection = useRef(null), feed = useRef(null), fileInput = useRef(null), composer = useRef(null);
  const snapshot = useRef(null), pollCount = useRef(0), historyPending = useRef(false);
  const eventGeneration = useRef(0);
  const resumePending = useRef(false);
  const [narrow, setNarrow] = useState(() => window.innerWidth < COMPACT_WIDTH);
  const [mobile, setMobile] = useState(() => window.innerWidth < 640);
  useEffect(() => {
    const resize = () => {setViewportWidth(window.innerWidth); setNarrow(window.innerWidth < COMPACT_WIDTH); setMobile(window.innerWidth < 640); paneDrag.current=null; setResizingPane(null);};
    const escape = e => {
      if (e.key === 'Escape' && !e.defaultPrevented) setSidebar(false);
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === 'p') {
        e.preventDefault(); setNavigator('workspace'); setNavigatorOpen(true); if (window.innerWidth < COMPACT_WIDTH) setSidebar(true);
        requestAnimationFrame(() => document.querySelector('[aria-label="Quick open workspace files"]')?.focus());
      }
    };
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
  const invalidateHistory = useCallback(() => {
    eventGeneration.current++;
    snapshot.current = null; setData(null);
    historyPending.current = false; setHistoryBusy(false);
  }, []);
  const acceptEventPage = useCallback((id, generation, page, options = {}) => {
    if (selection.current !== id || eventGeneration.current !== generation) return null;
    const before = snapshot.current, epoch = before?.history_epoch ?? 0;
    if ((page.history_epoch ?? epoch) < epoch) return null;
    const reset = !!before && (page.history_epoch ?? epoch) > epoch;
    if (reset) {
      // Invalidate outstanding refreshes and older-page requests in this tab.
      eventGeneration.current++;
      historyPending.current = false; setHistoryBusy(false);
    }
    const merged = mergeEventPage(before, page, options);
    snapshot.current = merged; setData(merged);
    return {reset, snapshot: merged};
  }, []);
  const refresh = useCallback(async id => {
    if (!id) return;
    const generation = eventGeneration.current;
    const metadataOnly = selection.current === id && !!snapshot.current;
    const query = metadataOnly ? `?events=0&history_epoch=${snapshot.current.history_epoch ?? 0}` : '';
    try {
      const result = await api(`/api/requests/${encodeURIComponent(id)}${query}`);
      if (acceptEventPage(id, generation, result, {snapshot: !metadataOnly})) return result;
    } catch (error) {
      if (selection.current === id && eventGeneration.current === generation) throw error;
    }
  }, [api, acceptEventPage]);
  const choose = useCallback((id, force = false) => {
    if ((!force && busy) || importPending || (id && id === selection.current)) return;
    setDrafts(prev => ({...prev, [selection.current || 'new']: {text, uploads, references}}));
    setReferences(drafts[id || 'new']?.references || []); setPicker(null);
    setText(drafts[id || 'new']?.text || ''); setUploads(drafts[id || 'new']?.uploads || []); selection.current = id;
    setSelected(id); setMobileView('chat'); setChatShown(true); setData(null); setError(''); setInfo(''); setQIndex(0); setOutputId(null); setSidebar(false); setHistoryAction(null);
    invalidateHistory(); setIsNearBottom(true);
    setVisibleMessages(40); setHistoryBusy(false); historyPending.current = false;
    if (id) {location.hash = id; setLoading(true); refresh(id).catch(e => setError(e.message)).finally(() => setLoading(false));}
    else {history.replaceState(null, '', location.pathname); setLoading(false); composer.current?.focus();}
  }, [text, uploads, references, drafts, refresh, busy, importPending, invalidateHistory]);
  const featureQuery = picker?.kind === 'path' ? picker.query : '';
  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    setFeatures({skills: [], folders: [], files: []}); setFeaturesLoading(true); setFeaturesError('');
    const timer = setTimeout(() => {
      const base = selected ? `/api/requests/${encodeURIComponent(selected)}/features` : '/api/features';
      api(`${base}${featureQuery ? `?q=${encodeURIComponent(featureQuery)}` : ''}`, {signal: controller.signal})
        .then(result => {if (live) setFeatures(result);})
        .catch(e => {if (live && e.name !== 'AbortError') setFeaturesError(e.message);})
        .finally(() => {if (live) setFeaturesLoading(false);});
    }, featureQuery ? 180 : 0);
    return () => {live = false; clearTimeout(timer); controller.abort();};
  }, [selected, api, picker?.kind, featureQuery, featuresVersion]);
  useEffect(() => {
    let live = true;
    (async () => {
      const bootstrap = await api('/api/bootstrap'); csrf.current = bootstrap.csrf_token;
      const result = await api('/api/requests'); if (!live) return;
      setRequests(result.requests || []);
      setProjects(result.projects || []);
      const hash = decodeURIComponent(location.hash.slice(1));
      const id = result.requests?.find(r => r.id === hash)?.id || result.requests?.[0]?.id;
      if (id) {selection.current = id; setSelected(id); await refresh(id);}
      setLoading(false);
    })().catch(e => {if (live) {setError(e.message); setLoading(false);}});
    return () => {live = false;};
  }, [api, refresh]);
  useEffect(() => {
    let live = true, timer, catchingUp = false;
    const controller = new AbortController();
    const poll = async () => {
      const generation = eventGeneration.current;
      try {
        if (!document.hidden) {
          pollCount.current++;
          if (selected && selection.current === selected && snapshot.current) {
            const before = snapshot.current;
            const next = await api(`/api/requests/${encodeURIComponent(selected)}/events?after=${before.cursor || 0}&history_epoch=${before.history_epoch ?? 0}`, {signal: controller.signal});
            if (live && selection.current === selected) {
              const accepted = acceptEventPage(selected, generation, next);
              catchingUp = !!accepted && !!next.events_after_has_more;
              if (accepted && (before?.active?.id !== next.active?.id || before?.last_job?.id !== next.last_job?.id || pollCount.current % 8 === 0)) await refresh(selected);
            }
          } else if (selected && selection.current === selected) await refresh(selected);
          if (pollCount.current % 4 === 0) {const result = await api('/api/requests', {signal: controller.signal}); if (live) {setRequests(result.requests || []); setProjects(result.projects || []);}}
          if (live) setError(previous => previous.startsWith('Connection interrupted.') ? '' : previous);
        } else catchingUp = false;
      } catch (e) {if (live && selection.current === selected && eventGeneration.current === generation) setError(`Connection interrupted. Retrying automatically. ${e.message}`);}
      if (live) timer = setTimeout(poll, catchingUp ? 200 : 1800);
    };
    timer = setTimeout(poll, 1800);
    return () => {live = false; clearTimeout(timer); controller.abort();};
  }, [selected, api, refresh, acceptEventPage]);
  const loadEarlierActivity = async () => {
    const id = selection.current, before = snapshot.current?.events_before;
    if (!id || !before || historyPending.current) return;
    const generation = eventGeneration.current, epoch = snapshot.current.history_epoch ?? 0;
    historyPending.current = true; setHistoryBusy(true); setIsNearBottom(false);
    const height = feed.current?.scrollHeight || 0, top = feed.current?.scrollTop || 0;
    try {
      const page = await api(`/api/requests/${encodeURIComponent(id)}/events?before=${before}&limit=300&history_epoch=${epoch}`);
      const accepted = acceptEventPage(id, generation, page, {older: true});
      if (!accepted || accepted.reset) return;
      requestAnimationFrame(() => {if (selection.current === id && eventGeneration.current === generation && feed.current) feed.current.scrollTop = top + feed.current.scrollHeight - height;});
    } catch (error) {if (selection.current === id && eventGeneration.current === generation) setError(`Earlier activity could not be loaded. ${error.message}`);}
    finally {if (selection.current === id && eventGeneration.current === generation) {historyPending.current = false; setHistoryBusy(false);}}
  };
  useEffect(() => {
    if (!selected) return;
    let live = true;
    const controller = new AbortController();
    api(`/api/requests/${encodeURIComponent(selected)}/imports`, {signal: controller.signal}).then(result => {
      if (!live || selection.current !== selected) return;
      const row = (result.jobs || []).find(item => ['queued', 'running'].includes(item.status));
      if (row && !localImportRef.current) {setImportPending(true); setLocalImport({path: row.path || row.name || '', job: {requestId: selected, row}});}
    }).catch(error => {if (live && error.name !== 'AbortError') setError(`Local import status could not be checked. ${error.message}`);});
    return () => {live = false; controller.abort();};
  }, [selected, api]);
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
  const inlineAnswers = (data?.asks || []).filter(q => q.answer?.delivery === 'waiting_tool').map(q => {
    const label = (q.options || []).find(o => o.id === q.answer.option)?.label || q.answer.option || '';
    return {role: 'human', ts: q.answer.at, text: `Answer to: ${q.question}\n${label}${q.answer.text ? '\n' + q.answer.text : ''}`};
  });
  const chat = [...(data?.request?.chat || []), ...inlineAnswers].sort((a, b) => a.ts.localeCompare(b.ts)), active = data?.active, queue = (data?.queue || []).filter(j => ['queued', 'pending'].includes(j.status));
  const chatBlocks = useMemo(() => new Map((data?.request?.chat || []).filter(message => message.role !== 'human')
    .map(message => [message, blocks(message.text)])), [data?.request?.chat]);
  const questions = (data?.asks || []).filter(q => ['open', 'defaulted'].includes(q.status));
  const question = questions[Math.min(qIndex, questions.length - 1)];
  const answering = !!question && !questionsHidden && questionModes[question.id] !== 'message';
  const answer = answering ? answers[question.id] : null;
  const waitingForAnswer = answering && questionWaiting(question);
  const composerText = answering ? questionDrafts[question.id] || '' : text;
  const syncPicker = (value = composerText, caret = composer.current?.selectionStart ?? value.length) => {
    setPicker(answering ? null : triggerAt(value, caret)); setPickIndex(0);
  };
  const setComposerText = value => {
    if (answering) {setQuestionDrafts(prev => ({...prev, [question.id]: value})); setPicker(null);}
    else {setText(value); syncPicker(value);}
  };
  const referenceOptions = matchingOptions(features, picker, references);
  const options = picker?.kind === 'path' ? [...referenceOptions, {kind: 'local_import', id: 'local', name: localPath(picker.query) ? 'Import this local path…' : 'Import a local path…', description: 'Copy a folder or file from your Mac into this workspace'}] : referenceOptions;
  const referenceLimit = features.max_references || 100;
  const addReference = option => {
    if (references.some(r => r.kind === option.kind && r.id === option.id)) return true;
    if (references.length >= referenceLimit) {setError(`Choose up to ${referenceLimit} references. Select a folder to include its files as one reference.`); return false;}
    setReferences(prev => prev.some(r => r.kind === option.kind && r.id === option.id) ? prev : [...prev, option]);
    return true;
  };
  const pick = option => {
    if (!picker || !option) return;
    if (option.kind === 'local_import') {openLocalImport(); return;}
    if (!addReference(option)) return;
    const next = composerText.slice(0, picker.start) + composerText.slice(picker.end);
    setText(next);
    setPicker(null);
    requestAnimationFrame(() => {composer.current?.focus(); composer.current?.setSelectionRange(picker.start, picker.start);});
  };
  const dropReference = event => {
    const raw = event.dataTransfer.getData('application/x-vwb-workspace-file');
    if (!raw) return false;
    event.preventDefault();
    if (answering) {setError('Choose New instruction to reference workspace files in a message.'); return true;}
    const option = workspaceReference(raw, selected);
    if (!option) {setError('Choose a file or folder inside this conversation’s work/ workspace.'); return true;}
    if (addReference(option)) {setPicker(null); composer.current?.focus();}
    return true;
  };
  const openLocalImport = () => {
    if (importPending) return;
    if (references.length >= referenceLimit) {setError(`Remove a reference before importing another path. You can select up to ${referenceLimit}.`); return;}
    setError(''); setInfo('');
    setLocalImport({path: localPath(picker?.query), trigger: picker?.kind === 'path' ? {...picker, text: text.slice(picker.start, picker.end)} : null});
    setPicker(null);
  };
  const ensureImportWorkspace = async () => {
    if (selection.current) return selection.current;
    const result = await api('/api/workspaces', {method: 'POST', body: '{}'});
    const id = result.request_id;
    if (!id) throw new Error('The workspace could not be prepared. Try importing again.');
    selection.current = id; setSelected(id); invalidateHistory();
    location.hash = id; await refresh(id);
    const list = await api('/api/requests'); setRequests(list.requests || []);
    return id;
  };
  const importedReference = (reference, requestId, row) => {
    if (selection.current !== requestId) return;
    const added = addReference(reference);
    const trigger = localImport?.trigger;
    if (trigger) setText(previous => previous.slice(trigger.start, trigger.end) === trigger.text ? previous.slice(0, trigger.start) + previous.slice(trigger.end) : previous);
    setFeaturesVersion(version => version + 1);
    window.dispatchEvent(new CustomEvent('vwb-workspace-changed', {detail: {requestId}}));
    setInfo(`Imported ${number(row.files || 0)} files${row.skipped ? `; ${number(row.skipped)} skipped` : ''}. ${added ? 'The path is selected for your next message.' : 'Select the imported path with @ after removing another reference.'}`);
    setLocalImport(null); setImportPending(false); composer.current?.focus();
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
  useEffect(() => {if (chatTab === 'edit' && isNearBottom && feed.current) feed.current.scrollTop = feed.current.scrollHeight;}, [chatTab, chat.length, data?.cursor, queue.length, selected, loading]);
  const mutate = async (path, body, method = 'POST') => {
    setBusy(true); setError(''); setInfo('');
    try {const result = await api(`/api/requests/${encodeURIComponent(selected)}${path}`, {method, body: JSON.stringify(body || {})}); await refresh(selected); return result;}
    catch (e) {setError(e.message); return null;}
    finally {setBusy(false);}
  };
  const refreshNavigation = async () => {
    const result = await api('/api/requests');
    setRequests(result.requests || []); setProjects(result.projects || []);
    return result;
  };
  const createProject = async ({name}) => {
    if (!projectKeys.current.has(name)) projectKeys.current.set(name, crypto.randomUUID());
    const result = await api('/api/projects', {method:'POST', body:JSON.stringify({name, operation_id:projectKeys.current.get(name)})});
    projectKeys.current.delete(name);
    await refreshNavigation();
    return result;
  };
  const createConversation = async projectId => {
    setBusy(true);
    try {
      const result = await api('/api/workspaces', {method:'POST', body:JSON.stringify({project_id:projectId})});
      await refreshNavigation(); choose(result.request_id, true);
      return result;
    } finally {setBusy(false);}
  };
  const moveConversation = async (requestId, projectId) => {
    setBusy(true);
    try {
      const result = await api(`/api/requests/${encodeURIComponent(requestId)}/project`, {method:'POST', body:JSON.stringify({project_id:projectId})});
      await refreshNavigation();
      if (selection.current === requestId) {invalidateHistory(); setCanvases(previous => ({...previous, [requestId]:initialCanvas()})); await refresh(requestId); setFeaturesVersion(value => value + 1);}
      return result;
    } finally {setBusy(false);}
  };
  const openHistory = (operation, message) => {
    setHistoryError('');
    setHistoryAction({operation, message, requestId:selected, revision:data?.request?.history_revision});
  };
  const submitHistory = async payload => {
    const target = historyAction;
    if (!target || target.requestId !== selection.current || busy) return false;
    const signature = JSON.stringify([target.requestId, target.revision, payload]);
    if (!historyKeys.current.has(signature)) historyKeys.current.set(signature, crypto.randomUUID());
    setBusy(true); setHistoryError('');
    try {
      const result = await api(`/api/requests/${encodeURIComponent(target.requestId)}/history`, {method:'POST', body:JSON.stringify({
        ...payload, expected_revision:target.revision, operation_id:historyKeys.current.get(signature)})});
      if ((result.request_id || target.requestId) === selection.current) invalidateHistory();
      historyKeys.current.delete(signature);
      await refreshNavigation(); setHistoryAction(null);
      const id = result.request_id || target.requestId;
      if (id !== selection.current) choose(id, true);
      else {await refresh(id); setIsNearBottom(true);}
      return result;
    } catch (failure) {setHistoryError(failure.message); return false;}
    finally {setBusy(false);}
  };
  const resume = async body => {
    if (busy || resumePending.current || active) return;
    resumePending.current = true;
    try {await mutate('/resume', body);}
    finally {resumePending.current = false;}
  };
  const send = async (value = composerText, mode = 'queue') => {
    if (picker && !answering) return;
    if (busy || importPending || (!value.trim() && !answer && (!uploads.length || answering))) return;
    if (!selected) {
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
    if (result) {if (isAnswer) {setQuestionDrafts(prev => ({...prev, [question.id]: (prev[question.id] || '').trim() === value.trim() ? '' : prev[question.id]})); setAnswers(prev => ({...prev, [question.id]: prev[question.id] === answer ? null : prev[question.id]}));} else {setText(previous => previous.trim() === value.trim() ? '' : previous); setUploads(previous => previous.filter(item => !uploads.some(sent => sent.id === item.id)));} setIsNearBottom(true);}
    if (result?.delivery === 'waiting_tool') setInfo('Answer delivered to the waiting agent.');
    else if (result?.delivery === 'interrupt_then_resume') setInfo('Continuing with your answer after the current turn stops.');
    else if (result && isAnswer) setInfo('Answer saved and queued.');
  };
  const upload = async files => {
    if (busy || importPending || !files.length) return;
    if (uploads.length + files.length > 12) {setError('Upload up to 12 files per message. For bulk work, type @ and choose Import a local path to bring in a whole folder as one reference.'); return;}
    if (files.some(file => file.size > 4 * 1024 ** 3)) {setError('Each attachment must be no larger than 4 GB.'); return;}
    setBusy(true); setError('');
    try {
      for (const file of files) {
        const result = await api('/api/uploads', {method: 'POST', raw: true, headers: {'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)}, body: file});
        setUploads(prev => [...prev, {...result.upload, id: result.id || result.upload?.id, name: file.name}]);
      }
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  };
  const compact = async () => {
    setCompactBusy(true); const result = await mutate('/compact'); setCompactBusy(false);
    if (result) setInfo('Compaction requested. Your conversation and files are kept.');
  };
  const current = requests.find(r => r.id === selected);
  const name = selected ? title(data?.request?.sources?.[0]?.name || current?.title || 'Conversation') : 'New conversation';
  const recovery = turnRecovery(data);
  const interrupted = recovery.job ? `${recovery.job.status === 'cancelled' ? 'The continuation was cancelled.' : `${recovery.job.kind === 'compact' ? 'Compaction' : 'The last turn'} ${recovery.job.status === 'failed' ? 'was interrupted by an error.' : 'stopped before it finished.'}`} Your conversation and saved work are kept.${recovery.job.error ? ` ${recovery.job.error}` : ''}` : '';
  const failure = !active && data?.last_job?.status === 'failed' ? (data.last_job.error || 'The last turn failed. Send a message to try again.') : '';
  const recoveryActions = html`<${RecoveryActions} recovery=${recovery} busy=${busy} onResume=${resume}/>`;
  const notice = error || interrupted || failure ? {tone: error || recovery.job?.status === 'failed' || failure ? 'error' : 'info', text: html`<span>${error || interrupted || failure}${recoveryActions}</span>`} : recovery.queueCount ? {tone: 'info', text: html`<span>Queue paused.${recoveryActions}</span>`} : info ? {tone: 'info', text: info} : active ? {tone: 'progress', text: active.kind === 'compact' ? 'Compacting your conversation…' : questions.some(questionWaiting) ? 'An agent is waiting for your answer. Other work can continue.' : 'The agent is working. You can keep writing.'} : questions.length ? {tone: 'info', text: `${questions.length} ${questions.length === 1 ? 'choice is' : 'choices are'} ready for your input.`} : null;
  const messageEvents = useMemo(() => activityIndex(data?.events), [data?.events]);
  const latestHuman = [...chat].reverse().find(m => m.role === 'human');
  // Eeva Surface does not forward style, so pane variables belong on a native element.
  return html`<${Surface} theme="editorial" background="chat">
    <div className=${`workbench canvas-workbench ${navigatorOpen ? 'navigator-open' : ''} ${sidebar ? 'nav-open' : ''} ${chatShown ? 'chat-shown' : 'chat-hidden'} mobile-view-${mobileView}`} style=${{'--chat-width': `${layout.chat}px`, '--navigator-width': `${layout.navigator}px`}}>
    ${resizingPane && html`<div className="pane-resize-shield" aria-hidden="true"></div>`}
    <a href=${chatTab === 'qc' ? '#qc-feed' : '#conversation-feed'} className="skip-link">Skip to conversation</a>
    <nav className="activity-rail" aria-label="Workbench sections"><div className="rail-brand" aria-label="Solstice video workbench"><${SolsticeMark} size=${24}/></div>
      <${Tooltip} content="Conversations"><button aria-label="Conversations" aria-pressed=${navigator === 'conversations' && (narrow ? sidebar : navigatorOpen)} onClick=${() => toggleNavigator('conversations')}><${Icon} name="library" size=${18}/></button><//>
      <${Tooltip} content="Workspace files"><button aria-label="Workspace files" aria-pressed=${navigator === 'workspace' && (narrow ? sidebar : navigatorOpen)} onClick=${() => toggleNavigator('workspace')}><${Icon} name="folder" size=${18}/></button><//>
      <${Tooltip} content="Image studio"><button aria-label="Open image studio" onClick=${() => openCanvas(studioTab('image'))}><${Icon} name="sparkles" size=${18}/></button><//>
      <${Tooltip} content="Video studio"><button aria-label="Open video studio" onClick=${() => openCanvas(studioTab('video'))}><${Icon} name="file" size=${18}/></button><//>
      <div className="rail-bottom"><${Tooltip} content="Local workbench"><span tabIndex="0" aria-label="Local workbench"><span className="local-dot"></span></span><//></div>
    </nav>
    ${narrow && sidebar && html`<button className="nav-scrim" aria-label="Close navigator" onClick=${() => setSidebar(false)}></button>`}
    <nav className="sidebar workspace-navigator" aria-label=${navigator === 'workspace' ? 'Workspace navigator' : 'Conversations navigator'} inert=${(narrow ? !sidebar : !navigatorOpen) ? '' : undefined}>
      ${navigator === 'conversations' ? html`<div className="navigator-heading"><h2>Conversations</h2><${IconButton} name="panel-right-close" label="Collapse navigator" onClick=${() => narrow ? setSidebar(false) : setNavigatorOpen(false)}/></div>
      <label className="conversation-search"><${Icon} name="search" size=${14}/><input value=${query} onChange=${e => setQuery(e.target.value)} placeholder="Find a conversation" aria-label="Find a conversation"/></label>
      <${ProjectNavigation} projects=${projects} requests=${requests} selected=${selected} query=${query} busy=${busy || importPending}
        onChoose=${choose} onCreateConversation=${createConversation} onCreateProject=${createProject} onMoveConversation=${moveConversation}/>
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
        <${Tabs} value=${chatTab} onValueChange=${setChatTab} className="chat-mode-tabs">
        <div className="chat-pane-heading"><${TabList} aria-label="Conversation mode"><${Tab} value="edit">Edit<//><${Tab} value="qc">QC<//><//><span>${chatTab === 'qc' ? 'Quality review' : active ? 'Working' : current?.output_count ? `${current.output_count} outputs` : 'Ready'}</span><${IconButton} name="chevron-left" label="Collapse chat" onClick=${() => narrow ? setMobileView('canvas') : setChatShown(false)}/></div>
        <${TabPanel} value="edit" forceMount hidden=${chatTab !== 'edit'} className="chat-mode-panel">
        ${notice && html`<div className="chat-panel-status"><${MessageBar} tone=${notice.tone}>${notice.text}<//></div>`}
        <div id="conversation-feed" className="conversation-feed" ref=${feed} tabIndex="0" onScroll=${e => setIsNearBottom(e.currentTarget.scrollHeight - e.currentTarget.scrollTop - e.currentTarget.clientHeight < 100)}>
          <div className="feed-inner">${loading ? html`<p className="loading-copy">Opening your conversation…</p>` : !chat.length ? html`<div className="welcome"><${SolsticeMark} size=${28}/><h2>Good work starts<br/>with a conversation.</h2><p>Start with an idea, a design, or any file.<br/>Tell me what you want to make.</p><div className="welcome-hint"><${Icon} name="file" size=${16}/><span>Describe your video or paste a Figma link to get started</span></div></div>` : html`
            <div className="conversation-start"><span>${date(data.request.created)}</span><span>One continuing conversation</span></div>
            ${(chat.length > visibleMessages || data?.events_has_more) && html`<div className="earlier-history"><p className="caption">Earlier messages and activity stay saved.</p><div>
              ${chat.length > visibleMessages && html`<button className="text-action" onClick=${() => {setIsNearBottom(false); setVisibleMessages(count => count + 40);}}>Show earlier messages</button>`}
              ${data?.events_has_more && html`<button className="text-action" disabled=${historyBusy} onClick=${loadEarlierActivity}>${historyBusy ? 'Loading earlier activity…' : 'Load earlier activity'}</button>`}
            </div></div>`}
            ${chat.slice(-visibleMessages).map((m, index) => {const i = Math.max(0, chat.length - visibleMessages) + index; return m.role === 'human' ? html`<div key=${m.id || `chat-${i}`}><${ReferenceTags} context=${m.context}/><${ChatAttachments} items=${m.attachments} onOpen=${openCanvas}/><${UserMessage} time=${time(m.ts)}>${m.text}<//><${MessageActions} message=${m} disabled=${busy || !!active || queue.length > 0} onAction=${openHistory}/>${((chat[i + 1] && chat[i + 1].role === 'human') || (!chat[i + 1] && !active)) && html`<${Activity} events=${messageEvents(m.ts, chat[i + 1]?.ts)} state=${data?.last_job?.status === 'failed' ? 'failed' : 'stopped'}/>`}</div>` : html`<div className="agent-turn" key=${m.id || `chat-${i}`}>
              <${Activity} events=${messageEvents(chat[i - 1]?.ts, m.ts)}/><${AgentMessage} blocks=${chatBlocks.get(m) || []} time=${time(m.ts)}/><${MessageActions} message=${m} disabled=${busy || !!active || queue.length > 0} onAction=${openHistory}/></div>`;})}
            ${active && html`<${Activity} events=${messageEvents(latestHuman?.ts)} working started=${active.started_at || active.created_at}/>`}
            ${queue.map(j => html`<div key=${j.id}><${ReferenceTags} context=${j.context}/><${ChatAttachments} items=${j.attachment_refs} onOpen=${openCanvas}/><${UserMessage} queued onRemoveQueued=${() => mutate(`/queue/${j.id}`, {}, 'DELETE')}>${j.message || j.text}<//></div>`)}
          `}</div>
        </div>
        ${!isNearBottom && html`<div className="jump-row"><${Button} variant="secondary" size="sm" onClick=${() => {feed.current.scrollTo({top: feed.current.scrollHeight, behavior: 'smooth'}); setIsNearBottom(true);}}>Jump to latest <${Icon} name="chevron-down" size=${14}/><//></div>`}
        <div className="composer-dock"><div className="composer-width"><div className="composer-shell" ref=${pickerShell} onKeyDownCapture=${pickerKeys}
          onDragOver=${e => {if (Array.from(e.dataTransfer.types || []).some(type => ['Files', 'application/x-vwb-workspace-file'].includes(type))) {e.preventDefault(); e.dataTransfer.dropEffect = 'copy';}}}
          onDrop=${e => {if (!dropReference(e) && e.dataTransfer.files.length) {e.preventDefault(); upload([...e.dataTransfer.files]);}}}
          onPaste=${e => {const files = [...(e.clipboardData?.files || [])].filter(file => file.type.startsWith('image/')); if (files.length) {e.preventDefault(); upload(files);}}}
          onKeyUp=${e => {if (e.target === composer.current && !['Escape', 'Enter', 'Tab', 'ArrowUp', 'ArrowDown'].includes(e.key)) syncPicker();}}
          onClick=${e => {if (e.target === composer.current) syncPicker();}}>
          ${picker && !answering && html`<div className="reference-dropup" aria-label=${picker.kind === 'skill' ? 'Available skills' : 'Workspace paths'}>
            <div className="reference-heading"><span>${picker.kind === 'skill' ? 'Features · Skills' : 'Files and folders'}</span><span className="caption">↑ ↓ to choose · Enter to add</span></div>
            <div id="composer-reference-options" role="listbox" aria-label=${picker.kind === 'skill' ? 'Choose a skill' : 'Choose a file or folder'} aria-busy=${featuresLoading}>
              ${options.map((option, i) => html`<button key=${`${option.kind}:${option.id}`} id=${`reference-option-${i}`} role="option" tabIndex="-1" aria-selected=${i === Math.min(pickIndex, options.length - 1)}
                className=${`reference-option ${i === Math.min(pickIndex, options.length - 1) ? 'highlighted' : ''}`} onMouseDown=${e => e.preventDefault()} onClick=${() => pick(option)}>
                <${Icon} name=${option.kind === 'skill' ? 'sparkles' : option.kind === 'file' ? 'file' : 'folder'} size=${16}/><span><strong title=${option.name}>${['file', 'folder'].includes(option.kind) ? displayPath(option.name) : option.name}</strong><span>${option.description}</span></span><//>`)}
              ${!referenceOptions.length && html`<p className="reference-empty" role="status">${featuresLoading ? 'Finding references…' : featuresError || (picker.kind === 'path' && !selected ? 'Import a local path to start a workspace, or choose an existing conversation.' : 'No matching references. Try a more specific path.')}</p>`}
            </div><div className="reference-footer">${picker.kind === 'skill' ? 'Choose a skill, then describe what you want to do.' : 'A folder includes all its files as one reference. The agent reads them as needed. Use @"work/folder name" for spaces.'}${features.paths_truncated && picker.kind === 'path' ? ' More paths available; refine your search.' : ''}
            </div>
          </div>`}
          ${!answering && references.length > 0 && html`<div className="reference-chips" role="group" aria-label="Selected references">${references.map(r => html`<span className="reference-chip" key=${`${r.kind}:${r.id}`}>
            <${Icon} name=${r.kind === 'skill' ? 'sparkles' : r.kind === 'plugin' ? 'library' : r.kind === 'file' ? 'file' : 'folder'} size=${13}/><${Tooltip} content=${r.name}><span title=${r.name}>${r.kind === 'skill' ? `/features ${r.name}` : r.kind === 'plugin' ? r.name : `@${displayPath(r.name)}`}</span><//>
            <${IconButton} name="x" label=${`Remove ${r.name}`} onClick=${() => setReferences(prev => prev.filter(x => x.kind !== r.kind || x.id !== r.id))}/></span>`)}</div>`}
          <${Composer} ref=${composer} value=${composerText} onValueChange=${setComposerText} onSend=${value => send(value)} working=${!!active}
            canSend=${!busy && !importPending && (!picker || answering) && (!!composerText.trim() || !!answer || (!answering && uploads.length > 0))} onStop=${active?.can_stop === false ? undefined : () => mutate('/stop')} onAttach=${answering || busy || importPending ? undefined : () => fileInput.current?.click()}
            label=${answering ? 'Your answer to the selected question' : 'Message the agent'}
            placeholder=${answering ? 'Choose an option, or write your answer…' : active ? 'Write a new instruction…' : selected ? 'What would you like to change?' : 'Describe a video or paste a Figma link…'}
            question=${questions.length ? html`<${QuestionQueue} questions=${questions} index=${qIndex} setIndex=${setQIndex} answer=${answers[question.id]} setAnswer=${value => {setAnswers(prev => ({...prev, [question.id]: value})); setQuestionModes(prev => ({...prev, [question.id]: 'answer'}));}} hidden=${questionsHidden} setHidden=${setQuestionsHidden} answering=${answering} onMode=${mode => {setQuestionModes(prev => ({...prev, [question.id]: mode})); setPicker(null); composer.current?.focus();}}/>` : undefined}
            attachments=${uploads.length ? html`<div className="staged-files">${uploads.map(u => html`<span className="staged-file" key=${u.id}>${u.kind === 'image' && u.url ? html`<img className="staged-image" src=${u.url} alt=""/>` : html`<${Icon} name="file" size=${14}/>`}${u.name}<${IconButton} name="x" label=${`Remove ${u.name}`} onClick=${() => setUploads(prev => prev.filter(x => x.id !== u.id))}/></span>`)}</div>` : answer ? html`<span className="selected-answer"><${Icon} name="check" size=${12}/> Answer selected. Send to confirm.</span>` : undefined}
            className="workbench-composer"/>
          <div className="composer-footer"><div className="composer-model"><${ModelControl} api=${api} requestId=${selected} onNotice=${notice => {if (notice.tone === 'error') setError(notice.text); else setInfo(notice.text);}}/><${IntegrationsControl} api=${api} disabled=${answering || busy} selected=${references.filter(r => r.kind === 'plugin')} onToggle=${item => {
              if (!references.some(r => r.kind === 'plugin' && r.id === item.id) && references.length >= referenceLimit) {setError(`Choose up to ${referenceLimit} references. Select a folder to include its files as one reference.`); return;}
              setReferences(previous => previous.some(r => r.kind === 'plugin' && r.id === item.id) ? previous.filter(r => r.kind !== 'plugin' || r.id !== item.id) : [...previous,item]);
            }}/>${(active || waitingForAnswer) && html`<span className="queue-label">${waitingForAnswer ? 'Send answers now' : answering ? 'Send queues answer' : 'Send queues instruction'}</span>`}</div>
            <div className="composer-footer-actions"><${SteeringAction} active=${active} answering=${answering} waiting=${waitingForAnswer} disabled=${busy || importPending || (picker && !answering) || (!composerText.trim() && !answer && (!uploads.length || answering))} onSend=${() => send(composerText, 'now')}/>
              <${ContextUsage} usage=${data?.usage} active=${active} pending=${queue.length > 0} compact=${compact} busy=${compactBusy}/></div></div>
        </div><div className="composer-help"><span>${busy ? 'Saving…' : answering ? `${waitingForAnswer ? 'Send resumes the waiting agent immediately.' : active ? (active.can_stop === false ? 'Send queues your answer behind the terminal turn.' : 'Send queues your answer. Answer now stops the turn and continues with your answer.') : 'Send continues with your answer.'}${uploads.length ? ' Attached files stay staged for New instruction.' : ''}` : active ? (active.can_stop === false ? 'This turn is running in a terminal. Your instruction will queue.' : 'Send now stops the current turn and continues with your instruction.') : 'Enter to send · Shift + Enter for a new line'}</span><span>${selected ? 'Changes stay in this conversation' : 'Source video optional'}</span></div></div></div>
        <input ref=${fileInput} className="visually-hidden" type="file" multiple onChange=${e => {upload([...e.target.files]); e.target.value = '';}} aria-label="Attach files"/>
        <//>
        <${TabPanel} value="qc" forceMount hidden=${chatTab !== 'qc'} className="chat-mode-panel">
          <${QCPanel} api=${api} requestId=${selected} visible=${chatTab === 'qc' && (narrow ? mobileView === 'chat' : chatShown)} Activity=${Activity}/>
        <//><//>
      </section>
      ${divider('chat')}
      <${WorkspaceCanvas} visible=${!narrow || mobileView === 'canvas'} api=${api} requestId=${selected} state=${canvas} dispatch=${dispatchCanvas} onExplore=${explore}
        renderOutputs=${() => html`<${OutputPanel} data=${data} outputId=${outputId} setOutputId=${setOutputId} onClose=${() => dispatchCanvas({type:'close',id:'outputs'})}/>`}/>

      </div>
    </main>
    <${LocalImportDialog} open=${!!localImport} initialPath=${localImport?.path || ''} initialJob=${localImport?.job || null} api=${api} ensureWorkspace=${ensureImportWorkspace} onReady=${importedReference} onPending=${setImportPending} onClose=${() => setLocalImport(null)}/>
    <${HistoryDialog} operation=${historyAction?.operation} message=${historyAction?.message} busy=${busy || !!active || queue.length > 0} error=${historyError} onSubmit=${submitHistory} onClose=${() => setHistoryAction(null)}/>
    </div>
  <//>`;
}

class ErrorBoundary extends React.Component {
  constructor(props) {super(props); this.state = {error: null};}
  static getDerivedStateFromError(error) {return {error};}
  render() {return this.state.error ? html`<main className="boot"><h1>The workbench couldn’t open</h1><p>${this.state.error.message}</p><button onClick=${() => location.reload()}>Reload workbench</button></main>` : this.props.children;}
}
ReactDOM.createRoot(document.getElementById('root')).render(html`<${ErrorBoundary}><${App}/><//>`);
