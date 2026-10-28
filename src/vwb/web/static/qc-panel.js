import htm from './vendor/htm.mjs';

const {createElement, useState, useEffect, useRef} = window.React;
const {Button, Composer, MessageBar, UserMessage, AgentMessage} = window.Eeva;
const html = htm.bind(createElement);
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
const integer = value => (typeof value === 'number' || typeof value === 'string' && value.trim() !== '') && Number.isSafeInteger(Number(value));
const stamp = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'}) : '';

// Match object-fit: contain and SVG's xMidYMid meet, including portrait media.
export function videoRect(viewport, width, height) {
  if (![viewport.width, viewport.height, width, height].every(n => Number.isFinite(n) && n > 0)) return null;
  const scale = Math.min(viewport.width / width, viewport.height / height);
  return {left:viewport.left + (viewport.width - width * scale) / 2,
    top:viewport.top + (viewport.height - height * scale) / 2, width:width * scale, height:height * scale};
}
export function videoPoint(point, rect, clip = false) {
  if (!rect || !point.every(Number.isFinite)) return null;
  const x = (point[0] - rect.left) / rect.width, y = (point[1] - rect.top) / rect.height;
  if (!clip && (x < 0 || x > 1 || y < 0 || y > 1)) return null;
  return [clamp(x, 0, 1), clamp(y, 0, 1)];
}
export function dragRegion(start, end, rect) {
  const a = videoPoint(start, rect), b = videoPoint(end, rect, true);
  if (!a || !b) return null;
  const round = n => Math.round(n * 1e6) / 1e6;
  const x = round(Math.min(a[0], b[0])), y = round(Math.min(a[1], b[1]));
  const w = round(Math.max(a[0], b[0])) - x, h = round(Math.max(a[1], b[1])) - y;
  return w > 0 && h > 0 ? [x, y, w, h] : null;
}
export function validRegion(region) {
  return Array.isArray(region) && region.length === 4 && region.every(n => typeof n === 'number' && Number.isFinite(n))
    && region[0] >= 0 && region[1] >= 0 && region[2] > 0 && region[3] > 0
    && region[0] + region[2] <= 1 && region[1] + region[3] <= 1;
}
export function frameAt(seconds, target) {
  if (!Number.isFinite(seconds) || !target || !(target.fps > 0) || !integer(target.frames) || target.frames < 1) return null;
  return clamp(Math.floor(Math.max(0, seconds) * target.fps + 1e-3), 0, target.frames - 1);
}
// Seek inside the frame; boundary timestamps can round down in media decoders.
export function frameTime(frame, target) {
  if (!integer(frame) || !target || !(target.fps > 0) || frame < 0 || frame >= target.frames) return null;
  return (Number(frame) + .5) / target.fps;
}
export function frameSelection(draft, target) {
  if (!target || !integer(target.frames) || target.frames < 1) throw new Error('Choose a rendered target with a known frame count.');
  if (!['frame', 'range'].includes(draft.selectionMode) || !integer(draft.f0)) throw new Error('Enter a whole start frame.');
  const f0 = Number(draft.f0), f1 = draft.selectionMode === 'frame' ? f0 + 1 : Number(draft.f1);
  if ((draft.selectionMode === 'range' && !integer(draft.f1)) || f0 < 0 || f0 >= target.frames || f1 <= f0 || f1 > target.frames) {
    throw new Error(`Use frames 0–${target.frames - 1}; the exclusive end must be after the start and at most ${target.frames}.`);
  }
  if (draft.region != null && !validRegion(draft.region)) throw new Error('Use a region [x, y, width, height] inside 0–1, with positive width and height.');
  return {target:target.target, f0, f1, ...(draft.region == null ? {} : {region:draft.region})};
}
export function sameTargetPin(a, b) {
  return !!a && !!b && a.target === b.target && !!a.sha256 && !!a.canvas_file_sha256
    && a.sha256 === b.sha256 && a.canvas_file_sha256 === b.canvas_file_sha256;
}
export function targetChanged(draft, targets) {
  return !!draft.reviewTarget && !sameTargetPin(draft.reviewTarget, targets.find(t => t.target === draft.target));
}
export function selectTarget(target) {
  return {target:target.target, reviewTarget:{...target}, selectionMode:'frame', f0:'0', f1:'1', region:null};
}
export function findingBody(draft, targets) {
  if (!draft.reviewTarget || draft.reviewTarget.target !== draft.target || targetChanged(draft, targets)) {
    throw new Error('The rendered target changed or has not been reviewed. Reload the target before saving a finding.');
  }
  const anchor = frameSelection(draft, draft.reviewTarget);
  if (draft.expected.length > 4000 || draft.observed.length > 4000) throw new Error('Keep each finding description within 4,000 characters.');
  if (!draft.expected.trim() || !draft.observed.trim()) throw new Error('Describe both what you expected and what you observed.');
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(draft.category.trim())) throw new Error('Use a lowercase category, such as visual, audio, or captions.');
  if (!['note', 'warning', 'blocker'].includes(draft.priority)) throw new Error('Choose note, warning, or blocker priority.');
  if (!['shared', 'audit'].includes(draft.lane)) throw new Error('Choose the shared or audit lane.');
  return {...anchor, pin:{sha256:draft.reviewTarget.sha256, canvas_file_sha256:draft.reviewTarget.canvas_file_sha256}, expected:draft.expected.trim(), observed:draft.observed.trim(), category:draft.category.trim(), priority:draft.priority, lane:draft.lane};
}
export function handoffItems(items, selected) {
  const ids = new Set(selected);
  return items.filter(item => ids.has(item.id) && item.lane === 'shared' && ['open', 'needs_recheck'].includes(item.status));
}
export function qcDraft() {
  return {text:'', uploads:[], target:'', reviewTarget:null, selectionMode:'frame', f0:'0', f1:'1', region:null,
    expected:'', observed:'', category:'visual', priority:'warning', lane:'shared', formOpen:false};
}

// One session per parent request. Late reads are fenced on hide/switch/mutation;
// late writes only update their owning session, even if another request is open.
export function createQCSession(requestId, api, {schedule = setTimeout, cancel = clearTimeout, uuid = () => crypto.randomUUID()} = {}) {
  const base = `/api/requests/${encodeURIComponent(requestId)}/qc`;
  let state = {data:null, draft:qcDraft(), selected:[], notes:{}, busy:false, uploading:false, error:'', pollError:'', info:''};
  let listener = null, generation = 0, timer = null, controller = null;
  const keys = new Map(), messageKeys = new Map();
  const update = patch => {state = {...state, ...patch}; listener?.();};
  const invalidate = () => {generation++; cancel(timer); timer = null; controller?.abort(); controller = null;};
  const refresh = async () => {
    invalidate();
    if (!listener || state.busy) return;
    const ticket = generation;
    controller = new AbortController();
    try {
      const data = await api(base, {signal:controller.signal});
      if (ticket !== generation || !listener) return;
      // QCWorkspace uses a private request id, while the route uses the parent id.
      if (data.request?.id !== `qc:${requestId}`) throw new Error('QC returned another conversation. Refresh to try again.');
      const firstTarget = !state.draft.target && data.targets?.[0];
      update({data, pollError:'', draft:firstTarget ? {...state.draft, ...selectTarget(firstTarget)} : state.draft});
    } catch (error) {
      if (ticket === generation && listener && error.name !== 'AbortError') update({pollError:error.message});
    } finally {
      if (ticket === generation && listener) timer = schedule(refresh, state.data?.active ? 1200 : 3000);
    }
  };
  const command = async (path, body = {}, method = 'POST', success = () => {}) => {
    if (state.busy || state.uploading) return null;
    invalidate(); update({busy:true, error:'', info:''});
    try {
      const result = await api(`${base}${path}`, {method, body:JSON.stringify(body)});
      success(result); return result;
    } catch (error) {update({error:error.message}); return null;}
    finally {update({busy:false}); refresh();}
  };
  const session = {
    get state() {return state;},
    start(onChange) {listener = onChange; refresh(); return () => {listener = null; invalidate();};},
    refresh,
    draft(patch) {update({draft:{...state.draft, ...patch}});},
    select(id, checked) {update({selected:checked ? [...new Set([...state.selected, id])] : state.selected.filter(x => x !== id)});},
    note(id, text) {update({notes:{...state.notes, [id]:text}});},
    error(message) {update({error:message});},
    command,
    async send(mode = 'queue') {
      const sent = state.draft;
      if (!sent.text.trim() && !sent.uploads.length) return;
      const message = sent.text.trim(), attachments = sent.uploads.map(file => file.id);
      const signature = JSON.stringify([message, attachments]);
      if (!messageKeys.has(signature)) messageKeys.set(signature, uuid());
      return command('/messages', {message, mode, attachments, idempotency_key:messageKeys.get(signature)}, 'POST', () => {
        messageKeys.delete(signature);
        session.draft({text:state.draft.text === sent.text ? '' : state.draft.text,
          uploads:state.draft.uploads.filter(file => !sent.uploads.some(old => old.id === file.id))});
      });
    },
    async upload(files) {
      if (state.uploading || state.busy || !files.length) return;
      if (files.some(file => file.size > 4 * 1024 ** 3)) {update({error:'Each QC attachment must be no larger than 4 GB.'}); return;}
      if (state.draft.uploads.length + files.length > 12) {update({error:'Attach up to 12 files per QC message.'}); return;}
      update({uploading:true, error:''});
      try {
        for (const file of files) {
          const result = await api('/api/uploads', {method:'POST', raw:true, headers:{'Content-Type':'application/octet-stream', 'X-Filename':encodeURIComponent(file.name)}, body:file});
          const id = result.id || result.upload?.id;
          if (!id) throw new Error('The file upload returned no attachment ID. Try again.');
          session.draft({uploads:[...state.draft.uploads, {...result.upload, id, name:file.name}]});
        }
      } catch (error) {update({error:error.message});}
      finally {update({uploading:false});}
    },
    async createFinding() {
      let body;
      try {body = findingBody(state.draft, state.data?.targets || []);}
      catch (error) {update({error:error.message}); return null;}
      const draft = state.draft;
      return command('/items', body, 'POST', () => {
        if (state.draft === draft) session.draft({expected:'', observed:'', formOpen:false});
        update({info:'Finding saved to QC.'});
      });
    },
    async transition(item, status) {
      return command(`/items/${encodeURIComponent(item.id)}/status`, {status, expected_revision:item.revision, note:state.notes[item.id] || ''}, 'POST', () => update({info:status === 'resolved' ? 'Finding resolved by human review.' : 'Finding status saved.'}));
    },
    async handoff() {
      const items = handoffItems(state.data?.items || [], state.selected);
      if (!items.length) {update({error:'Select open shared findings to send to the editor. Audit findings stay in QC.'}); return null;}
      const signature = JSON.stringify(items.map(item => [item.id, item.revision]).sort(([a], [b]) => a.localeCompare(b)));
      if (!keys.has(signature)) keys.set(signature, uuid());
      return command('/handoff', {item_ids:items.map(item => item.id), expected_revisions:Object.fromEntries(items.map(item => [item.id, item.revision])), idempotency_key:keys.get(signature)}, 'POST', () => {
        update({selected:state.selected.filter(id => !items.some(item => item.id === id)), info:`${items.length} ${items.length === 1 ? 'finding sent' : 'findings sent'} to the editor.`});
      });
    },
  };
  return session;
}

export function QCPanel({api, requestId, visible = true, Activity}) {
  const sessions = useRef(new Map()), [, redraw] = useState(0);
  if (requestId && !sessions.current.has(requestId)) sessions.current.set(requestId, createQCSession(requestId, api));
  const session = sessions.current.get(requestId);
  useEffect(() => {
    if (session && visible) return session.start(() => redraw(n => n + 1));
  }, [session, visible]);
  if (!requestId) return html`<div id="qc-feed" tabIndex="0" className="qc-empty"><h2>Review your video</h2><p>Start a conversation in Edit, then return here for a separate QC conversation and findings.</p></div>`;
  return html`<${QCSurface} key=${requestId} session=${session} requestId=${requestId} visible=${visible} Activity=${Activity}/>`;
}

function QCAttachments({files = [], requestId}) {
  return files.length ? html`<ul className="qc-attachments" aria-label="QC attachments">${files.map(file => html`<li key=${file.id}>
    <a href=${`/api/requests/${encodeURIComponent(requestId)}/qc/attachments/${encodeURIComponent(file.id)}`} target="_blank" rel="noopener noreferrer">${file.name || 'Attachment'}</a>
  </li>`)}</ul>` : null;
}

function ReviewPlayer({target, draft, setDraft, visible, onError}) {
  const video = useRef(null), drag = useRef(null);
  const [drawing, setDrawing] = useState(false), [preview, setPreview] = useState(null), [currentFrame, setCurrentFrame] = useState(0);
  useEffect(() => {if (!visible) {video.current?.pause(); drag.current = null; setPreview(null); setDrawing(false);}}, [visible]);
  const seek = frame => {
    const time = frameTime(frame, target);
    if (video.current && time != null) video.current.currentTime = time;
  };
  useEffect(() => {if (video.current?.readyState >= 1) seek(draft.f0);}, [target.target, draft.f0, draft.seek]);
  const stopDrag = (event, apply) => {
    const active = drag.current;
    if (!active || active.id !== event.pointerId) return;
    const region = dragRegion(active.start, [event.clientX, event.clientY], active.rect);
    drag.current = null; setPreview(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (apply && region) {setDraft({region}); setDrawing(false);}
  };
  const region = preview || draft.region;
  return html`<div className="qc-review-player">
    <div className=${`qc-video-stage ${drawing ? 'is-drawing' : ''}`} onPointerDown=${event => {
      if (!drawing || event.button !== 0 || event.currentTarget.closest('fieldset')?.disabled) return;
      const rect = videoRect(event.currentTarget.getBoundingClientRect(), target.width, target.height);
      if (!videoPoint([event.clientX, event.clientY], rect)) return;
      event.preventDefault(); video.current?.pause();
      drag.current = {id:event.pointerId, start:[event.clientX, event.clientY], rect};
      event.currentTarget.setPointerCapture(event.pointerId);
    }} onPointerMove=${event => {const active = drag.current; if (active?.id === event.pointerId) setPreview(dragRegion(active.start, [event.clientX, event.clientY], active.rect));}}
      onPointerUp=${event => stopDrag(event, true)} onPointerCancel=${event => stopDrag(event, false)} onLostPointerCapture=${() => {drag.current = null; setPreview(null);}}>
      <video ref=${video} src=${`${target.url}${target.url.includes('?') ? '&' : '?'}qc_pin=${encodeURIComponent(`${target.sha256}:${target.canvas_file_sha256}`)}`} controls=${!drawing} playsInline preload="metadata" aria-label=${`QC preview ${target.target}`}
        onLoadedMetadata=${() => seek(draft.f0)} onTimeUpdate=${event => setCurrentFrame(frameAt(event.currentTarget.currentTime, target) ?? 0)}
        onError=${() => onError('This rendered video could not load. Refresh QC and try the target again.')} />
      ${validRegion(region) && html`<svg className="qc-region-overlay" viewBox=${`0 0 ${target.width} ${target.height}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        <rect x=${region[0] * target.width} y=${region[1] * target.height} width=${region[2] * target.width} height=${region[3] * target.height} vector-effect="non-scaling-stroke"/>
      </svg>`}
    </div>
    <p className="qc-caption">${target.width} × ${target.height} · ${target.fps} fps · ${target.frames} frames · Pinned version</p>
    <div className="qc-actions"><${Button} variant="secondary" size="sm" onClick=${() => {video.current?.pause(); setDraft({f0:String(currentFrame), f1:String(currentFrame + 1), selectionMode:'frame'});}}>Use frame ${currentFrame}<//>
      <${Button} variant="text" size="sm" aria-pressed=${drawing} onClick=${() => {video.current?.pause(); setDrawing(!drawing);}}> ${drawing ? 'Cancel drawing' : 'Draw region'}<//>
    </div>
    ${drawing && html`<p className="qc-caption" role="status">Drag over the picture to mark a region. You can also enter coordinates below.</p>`}
    <fieldset className="qc-fields"><legend>Selected frames</legend>
      <label>Selection<select value=${draft.selectionMode} onChange=${event => setDraft({selectionMode:event.target.value})}><option value="frame">Single frame</option><option value="range">Frame range</option></select></label>
      <div className="qc-field-pair"><label>Start frame<input type="number" min="0" max=${target.frames - 1} step="1" value=${draft.f0} onChange=${event => setDraft({f0:event.target.value})}/></label>
      ${draft.selectionMode === 'range' && html`<label>End (exclusive)<input type="number" min="1" max=${target.frames} step="1" value=${draft.f1} onChange=${event => setDraft({f1:event.target.value})}/></label>`}</div>
      <p className="qc-caption">Frames start at 0. The end frame is excluded.</p>
      <button className="text-action" onClick=${() => seek(draft.f0)}>Go to start frame</button>
    </fieldset>
    <details className="qc-region-fields"><summary>${draft.region ? 'Region coordinates' : 'Add a region with coordinates'}</summary>
      <p className="qc-caption">Normalized to the video picture: x, y, width, height in 0–1.</p>
      <div className="qc-field-pair">${['x', 'y', 'width', 'height'].map((label, index) => html`<label key=${label}>Region ${label}<input type="number" min="0" max="1" step="0.01" value=${draft.region?.[index] ?? ''}
        onChange=${event => {const next = [...(draft.region || [0, 0, 1, 1])]; next[index] = event.target.value === '' ? '' : Number(event.target.value); setDraft({region:next});}}/></label>`)}</div>
    </details>
    ${draft.region && html`<button className="text-action" onClick=${() => setDraft({region:null})}>Clear region · use full frame</button>`}
  </div>`;
}

export function QCSurface({session, requestId, visible, Activity}) {
  const {data, draft, busy, uploading, selected, notes, error, pollError, info} = session.state;
  const feed = useRef(null), fileInput = useRef(null), nearBottom = useRef(true);
  const [reviewOpen, setReviewOpen] = useState(false);
  const active = data?.active, queue = data?.queue || [], items = data?.items || [], targets = data?.targets || [];
  const target = draft.reviewTarget, currentTarget = targets.find(item => item.target === draft.target), changed = targetChanged(draft, targets), locked = busy || uploading;
  const eligible = handoffItems(items, selected);
  const chat = data?.request?.chat || [], events = data?.events || [];
  useEffect(() => {if (visible && nearBottom.current && feed.current && !reviewOpen) feed.current.scrollTop = feed.current.scrollHeight;}, [chat.length, events.at(-1)?.seq, visible, reviewOpen]);
  const recovery = !active && data?.resumable_job;
  const failed = !active && data?.last_job?.status === 'failed';
  const notice = error || pollError || (locked ? uploading ? 'Uploading QC attachments…' : 'Saving QC changes…' : '')
    || (changed ? 'This target’s rendered bytes or composition changed. Reload the target and review it again.' : '') || info
    || (active ? 'QC is working. Send adds to its queue.' : data?.paused ? 'QC queue is paused.' : failed ? data.last_job.error || 'The QC turn failed.' : recovery ? 'QC stopped. Your conversation and findings are saved.' : !data ? 'Opening QC…' : '');
  let selectionError = '';
  if (target) {try {frameSelection(draft, target);} catch (error) {selectionError = error.message;}}
  const canSend = !!data && !locked && (!!draft.text.trim() || draft.uploads.length > 0);
  const setDraft = patch => session.draft(patch);
  return html`<section className="qc-panel" aria-label="Quality control">
    ${notice && html`<div className="chat-panel-status"><${MessageBar} tone=${error || pollError || failed ? 'error' : active || locked || !data ? 'progress' : 'info'}>
      <span>${notice}${pollError && html` <button className="text-action" onClick=${session.refresh}>Retry QC</button>`}</span><//></div>`}
    ${!active && (recovery || data?.paused && queue.length > 0) && html`<div className="qc-recovery qc-actions">
      ${recovery && html`<${Button} variant="secondary" size="sm" disabled=${locked} onClick=${() => session.command('/resume', {job_id:recovery.id})}>Resume QC turn<//>`}
      ${data?.paused && queue.length > 0 && html`<${Button} variant="text" size="sm" disabled=${locked} onClick=${() => session.command('/resume')}>Resume QC queue<//>`}
    </div>`}
    <div id="qc-feed" className="qc-feed" tabIndex="0" ref=${feed} onScroll=${event => {nearBottom.current = event.currentTarget.scrollHeight - event.currentTarget.scrollTop - event.currentTarget.clientHeight < 80;}}>
      <details className="qc-review" open=${reviewOpen} onToggle=${event => {if (event.target === event.currentTarget) setReviewOpen(event.currentTarget.open);}}>
        <summary>Review video & findings <span>${items.length}</span></summary>
        <div className="qc-review-body">
        ${targets.length ? html`<label>Rendered target<select value=${draft.target} disabled=${locked} onChange=${event => setDraft(selectTarget(targets.find(t => t.target === event.target.value)))}>
          ${!currentTarget && html`<option value=${draft.target}>Previously selected target unavailable</option>`}
          ${targets.map(t => html`<option value=${t.target} key=${t.target}>${t.target}</option>`)}</select></label>` : html`<div className="qc-empty"><h3>No rendered targets yet</h3><p>Render an output in Edit to review frames here. You can still chat with QC.</p></div>`}
        ${changed && html`<div className="qc-target-changed"><p className="qc-validation" role="status">The pinned target changed or is unavailable. Reload to review the latest bytes; this clears the frame and region selection.</p>
          <${Button} variant="secondary" size="sm" disabled=${locked || !currentTarget} onClick=${() => {session.error(''); setDraft(selectTarget(currentTarget));}}>Reload target for review<//></div>`}
        ${target && !changed && html`<fieldset className="qc-review-controls" disabled=${locked}><${ReviewPlayer} key=${`${target.target}:${target.sha256}:${target.canvas_file_sha256}`} target=${target} draft=${draft} setDraft=${setDraft} visible=${visible && reviewOpen} onError=${session.error}/>
          ${selectionError && html`<p className="qc-validation" role="status">${selectionError}</p>`}
          <details open=${draft.formOpen} onToggle=${event => {if (event.target === event.currentTarget && event.currentTarget.open !== draft.formOpen) setDraft({formOpen:event.currentTarget.open});}}>
            <summary>Add a finding</summary><form className="qc-finding-form" onSubmit=${event => {event.preventDefault(); session.createFinding();}}>
              <label>Expected<textarea required rows="2" maxLength="4000" value=${draft.expected} onChange=${event => setDraft({expected:event.target.value})}/></label>
              <label>Observed<textarea required rows="2" maxLength="4000" value=${draft.observed} onChange=${event => setDraft({observed:event.target.value})}/></label>
              <div className="qc-field-pair"><label>Category<input required pattern="[a-z][a-z0-9_-]{0,63}" value=${draft.category} maxLength="64" onChange=${event => setDraft({category:event.target.value})}/></label>
              <label>Priority<select value=${draft.priority} onChange=${event => setDraft({priority:event.target.value})}>${['note', 'warning', 'blocker'].map(value => html`<option key=${value} value=${value}>${value[0].toUpperCase() + value.slice(1)}</option>`)}</select></label></div>
              <label>Lane<select value=${draft.lane} onChange=${event => setDraft({lane:event.target.value})}><option value="shared">Shared · eligible for editor handoff</option><option value="audit">Audit · stays in QC</option></select></label>
              <${Button} type="submit" variant="secondary" size="sm" disabled=${locked || !!selectionError || !draft.expected.trim() || !draft.observed.trim()}>Save finding<//>
            </form>
          </details></fieldset>`}
        <section className="qc-findings" aria-label="QC findings"><h3>Findings (${items.length})</h3>
          <p className="qc-caption">Resolved means human reviewed. Findings and resolutions are not regulatory acceptance.</p>
          ${!items.length && html`<p className="qc-caption">No findings recorded yet.</p>`}
          ${items.map(item => html`<article className="qc-finding" key=${item.id}>
            <div className="qc-finding-top"><label className="qc-item-select"><input type="checkbox" checked=${selected.includes(item.id)} disabled=${locked || !handoffItems([item], [item.id]).length} onChange=${event => session.select(item.id, event.target.checked)} aria-label=${`Select finding ${item.id} for editor`}/><strong>${item.category} · ${item.priority}</strong></label><span>${item.lane === 'audit' ? 'Audit · private' : 'Shared'}</span></div>
            <p className=${`qc-item-status ${item.status === 'needs_recheck' ? 'needs-recheck' : ''}`}>${item.status === 'needs_recheck' ? 'Needs recheck · a newer version requires review' : item.status === 'resolved' ? 'Resolved · human reviewed' : item.status === 'dismissed' ? 'Dismissed by human' : item.status === 'open' ? 'Open' : item.status}</p>
            <p className="qc-caption">${item.anchor?.target} · [${item.anchor?.f0}, ${item.anchor?.f1})${item.anchor?.region ? ` · region [${item.anchor.region.map(n => Number(n).toFixed(3)).join(', ')}]` : ''}</p>
            <dl><dt>Expected</dt><dd>${item.expected}</dd><dt>Observed</dt><dd>${item.observed}</dd></dl>
            <button className="text-action" disabled=${locked || !targets.some(t => sameTargetPin(t, item.anchor))} onClick=${() => {setDraft({...selectTarget(targets.find(t => sameTargetPin(t, item.anchor))), f0:String(item.anchor.f0), f1:String(item.anchor.f1), selectionMode:item.anchor.f1 === item.anchor.f0 + 1 ? 'frame' : 'range', region:item.anchor.region || null, seek:(draft.seek || 0) + 1}); feed.current?.scrollTo({top:0});}}>Review pinned frames</button>
            <details><summary>Human review actions</summary><label>Review note (optional)<textarea rows="2" maxLength="4000" value=${notes[item.id] || ''} disabled=${locked} onChange=${event => session.note(item.id, event.target.value)}/></label>
              <div className="qc-actions">${item.status !== 'resolved' && html`<${Button} variant="secondary" size="sm" disabled=${locked} onClick=${() => session.transition(item, 'resolved')}>Resolve · human reviewed<//>`}
                ${item.status !== 'dismissed' && html`<${Button} variant="text" size="sm" disabled=${locked} onClick=${() => session.transition(item, 'dismissed')}>Dismiss<//>`}
                ${item.status !== 'open' && html`<${Button} variant="text" size="sm" disabled=${locked} onClick=${() => session.transition(item, 'open')}>Reopen<//>`}</div>
            </details>
          </article>`)}
          <${Button} variant="secondary" size="sm" disabled=${locked || !eligible.length} onClick=${() => session.handoff()}>Send selected to editor (${eligible.length})<//>
          <p className="qc-caption">Only selected open shared findings are sent. Audit findings stay in QC.</p>
        </section></div>
      </details>
      <div className="qc-chat" aria-label="QC conversation">
        ${data && !chat.length && html`<div className="qc-empty"><h2>A second look</h2><p>Ask QC to inspect your rendered video. Record findings here, then choose which shared findings to send to Edit.</p>${!targets.length && html`<p>No rendered targets yet. Render an output in Edit to begin frame review.</p>`}</div>`}
        ${chat.map((message, index) => html`<div key=${`${index}:${message.ts}`}><${QCAttachments} files=${message.attachments} requestId=${requestId}/>
          ${message.role === 'human' ? html`<${UserMessage} time=${stamp(message.ts)}>${message.text}<//>` : html`<${AgentMessage} blocks=${[{type:'text', text:message.text}]} time=${stamp(message.ts)}/>`}</div>`)}
        ${Activity && html`<${Activity} events=${events} working=${!!active} started=${active?.started_at || active?.created_at} state=${active ? 'working' : failed ? 'failed' : recovery || ['stopped', 'cancelled'].includes(data?.last_job?.status) ? 'stopped' : 'done'}/>`}
        ${queue.map(job => html`<div key=${job.id}><${QCAttachments} files=${job.attachment_refs} requestId=${requestId}/><${UserMessage} queued onRemoveQueued=${locked ? undefined : () => session.command(`/queue/${encodeURIComponent(job.id)}`, {}, 'DELETE')}>${job.message || job.text}<//></div>`)}
      </div>
    </div>
    <div className="composer-dock"><div className="composer-shell">
      <${Composer} value=${draft.text} onValueChange=${text => setDraft({text})} onSend=${() => session.send()} canSend=${canSend} working=${!!active}
        onStop=${locked || active?.can_stop === false ? undefined : () => session.command('/stop')}
        onAttach=${locked ? undefined : () => fileInput.current?.click()} label="Message QC" placeholder=${active ? 'Add a message to the QC queue…' : 'What should QC look for?'} className="workbench-composer"
        attachments=${draft.uploads.length ? html`<div className="staged-files">${draft.uploads.map(file => html`<span className="staged-file" key=${file.id}>${file.name}<button className="text-action" disabled=${locked} aria-label=${`Remove QC attachment ${file.name}`} onClick=${() => setDraft({uploads:draft.uploads.filter(other => other.id !== file.id)})}>Remove</button></span>`)}</div>` : undefined}/>
      <div className="qc-composer-footer"><span title="QC uses its separately configured model">${data?.model?.model || 'QC model not reported'}${data?.model?.provider ? ` · ${data.model.provider}` : ''}</span>
        ${active && active.can_stop !== false && html`<button className="text-action" disabled=${!canSend} onClick=${() => session.send('now')}>Send now</button>`}
      </div>
    </div><p className="qc-caption qc-composer-help">${active ? 'Send queues. Send now stops QC and continues with your message.' : 'Enter to send · Shift + Enter for a new line'}</p>
      <details className="qc-usage"><summary>QC usage</summary><p className="qc-caption">Context tokens: ${data?.usage?.context?.used_tokens ?? 'Not reported'} / ${data?.usage?.context?.limit_tokens ?? 'Not reported'}</p></details>
    </div>
    <input ref=${fileInput} className="visually-hidden" type="file" multiple aria-label="Attach QC files" onChange=${event => {session.upload([...event.target.files]); event.target.value = '';}}/>
  </section>`;
}
