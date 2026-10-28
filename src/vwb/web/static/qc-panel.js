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
export function newerTargets(item, targets) {
  const anchor = item?.anchor;
  return targets.filter(t => anchor?.output && t.output === anchor.output && integer(t.version)
    && integer(anchor.version) && Number(t.version) > Number(anchor.version))
    .sort((a, b) => b.version - a.version);
}
export function comparisonDraft(item, targets, target) {
  const after = target || newerTargets(item, targets)[0] || null;
  // Never translate a finding's frame numbers across different media clocks.
  const prefill = after && after.fps === item.anchor.fps && after.frames === item.anchor.frames;
  return {item:JSON.parse(JSON.stringify(item)), after:after ? {...after} : null,
    f0:prefill ? String(item.anchor.f0) : '', f1:prefill ? String(item.anchor.f1) : '', confirmed:false};
}
export function comparisonBody(comparison, items, targets) {
  if (!comparison) throw new Error('Choose a finding to compare.');
  const {item, after} = comparison, current = items.find(i => i.id === item.id), before = item.anchor;
  if (!current || current.revision !== item.revision || !sameTargetPin(current.anchor, before)) {
    throw new Error('This finding changed. Reload the comparison and confirm its window again.');
  }
  if (!sameTargetPin(before, targets.find(t => t.target === before.target))) {
    throw new Error('The original pin changed or is unavailable. Its recorded frames cannot be compared to replacement bytes.');
  }
  if (!after || !newerTargets(item, [after]).length || !newerTargets(item, targets).some(t => t.target === after.target)) {
    throw new Error('Choose a newer rendered version of the same output.');
  }
  if (!sameTargetPin(after, targets.find(t => t.target === after.target))) {
    throw new Error('The newer target pin changed. Reload the comparison and choose its window again.');
  }
  if (![before.fps, after.fps].every(fps => Number.isFinite(fps) && fps > 0)) throw new Error('Both targets need a known frame rate.');
  if ([before, after].some(pin => ['output', 'version', 'fps', 'frames', 'width', 'height'].some(key => pin[key] !== targets.find(t => t.target === pin.target)?.[key]))) {
    throw new Error('Target timing or dimensions changed. Reload the comparison before choosing a window.');
  }
  frameSelection({...before, selectionMode:'range'}, before);
  const {f0, f1} = frameSelection({...comparison, selectionMode:'range'}, after);
  return {item_id:item.id, expected_revision:item.revision, target:after.target,
    pin:{sha256:after.sha256, canvas_file_sha256:after.canvas_file_sha256}, f0, f1};
}
// Elapsed seconds from each explicitly selected start; the shorter window holds
// on its last frame. This is not content tracking or proportional range scaling.
export function comparisonFrames(before, after, seconds = 0) {
  for (const window of [before, after]) {
    frameSelection({...window, selectionMode:'range'}, window);
    if (!Number.isFinite(window.fps) || window.fps <= 0) throw new Error('A known frame rate is required.');
  }
  const max = Math.max((before.f1 - before.f0 - 1) / before.fps, (after.f1 - after.f0 - 1) / after.fps);
  const offset = clamp(Number.isFinite(seconds) ? seconds : 0, 0, max);
  const at = w => clamp(Number(w.f0) + Math.floor(offset * w.fps + 1e-7), Number(w.f0), Number(w.f1) - 1);
  return {before:at(before), after:at(after), offset, max, step:1 / Math.max(before.fps, after.fps)};
}
export function findingMarkers(items, target) {
  if (!target) return [];
  return items.filter(item => {
    if (!sameTargetPin(item.anchor, target)) return false;
    try {frameSelection({...item.anchor, selectionMode:'range'}, target); return true;} catch {return false;}
  }).map(item => ({item, left:100 * item.anchor.f0 / target.frames, width:100 * (item.anchor.f1 - item.anchor.f0) / target.frames}));
}
export function recheckStaleReason(recheck, item, targets) {
  if (recheck.stale) return recheck.stale_reason || 'The finding or its pinned media has changed.';
  if (!item || item.revision !== recheck.item_revision) return 'The finding changed after this recheck was prepared.';
  if (![recheck.before, recheck.after].every(pin => sameTargetPin(pin, targets.find(t => t.target === pin?.target)))) return 'Pinned media changed or is unavailable.';
  if (newerTargets(item, targets).some(t => t.version > recheck.after.version)) return 'A newer rendered version is available.';
  return '';
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
  let state = {data:null, draft:qcDraft(), comparison:null, comparisonOpen:false, selected:[], notes:{}, busy:false, uploading:false, error:'', pollError:'', info:''};
  let listener = null, generation = 0, timer = null, controller = null;
  let recheckAttempt = null;
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
    openComparison(id) {
      const item = state.data?.items?.find(i => i.id === id);
      if (!item) return;
      if (state.comparison?.item.id !== id) {
        recheckAttempt = null;
        update({comparison:comparisonDraft(item, state.data.targets || [])});
      }
      update({comparisonOpen:true, error:'', info:''});
    },
    closeComparison() {update({comparisonOpen:false});},
    reloadComparison() {
      const item = state.data?.items?.find(i => i.id === state.comparison?.item.id);
      if (!item) return;
      const targets = state.data.targets || [], after = newerTargets(item, targets).find(t => t.target === state.comparison?.after?.target);
      recheckAttempt = null;
      update({comparison:comparisonDraft(item, targets, after), error:''});
    },
    comparisonTarget(targetId) {
      if (!state.comparison) return;
      const target = newerTargets(state.comparison.item, state.data?.targets || []).find(t => t.target === targetId);
      if (!target || sameTargetPin(target, state.comparison.after)) return;
      recheckAttempt = null;
      update({comparison:comparisonDraft(state.comparison.item, state.data.targets, target), error:''});
    },
    comparisonWindow(patch) {
      if (!state.comparison) return;
      const next = {...state.comparison, ...Object.fromEntries(['f0', 'f1'].filter(k => k in patch).map(k => [k, patch[k]]))};
      if (next.f0 === state.comparison.f0 && next.f1 === state.comparison.f1) return;
      if (!['f0', 'f1'].every(k => integer(next[k]) && integer(state.comparison[k]) && Number(next[k]) === Number(state.comparison[k]))) recheckAttempt = null;
      update({comparison:{...next, confirmed:false}, error:''});
    },
    confirmComparison(confirmed) {
      if (!state.comparison) return;
      if (confirmed) {
        try {
          comparisonBody(state.comparison, state.data?.items || [], state.data?.targets || []);
          if (!['open', 'needs_recheck'].includes(state.comparison.item.status)) throw new Error('Reopen this finding before requesting another model recheck.');
        }
        catch (error) {update({error:error.message}); return;}
      }
      update({comparison:{...state.comparison, confirmed:!!confirmed}});
    },
    async recheck() {
      if (state.busy || state.uploading) return null;
      let body;
      try {
        body = comparisonBody(state.comparison, state.data?.items || [], state.data?.targets || []);
        if (!['open', 'needs_recheck'].includes(state.comparison.item.status)) throw new Error('Reopen this finding before requesting another model recheck.');
        if (!state.comparison.confirmed) throw new Error('Confirm the newer frame window before requesting a model recheck.');
      } catch (error) {update({error:error.message}); return null;}
      const signature = JSON.stringify(body);
      if (recheckAttempt?.signature !== signature) recheckAttempt = {signature, key:uuid()};
      const attempt = recheckAttempt, comparison = state.comparison;
      return command('/rechecks', {...body, idempotency_key:attempt.key}, 'POST', result => {
        if (recheckAttempt === attempt) recheckAttempt = null;
        if (state.comparison === comparison) update({comparison:{...comparison, confirmed:false}});
        update({info:`Recheck ${result.recheck?.id || ''} submitted to the QC queue. Finding status is unchanged.`});
      });
    },
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

const pinnedMediaUrl = target => `${target.url}${target.url.includes('?') ? '&' : '?'}qc_pin=${encodeURIComponent(`${target.sha256}:${target.canvas_file_sha256}`)}`;
function RegionOverlay({target, region}) {
  return validRegion(region) ? html`<svg className="qc-region-overlay" viewBox=${`0 0 ${target.width} ${target.height}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
    <rect x=${region[0] * target.width} y=${region[1] * target.height} width=${region[2] * target.width} height=${region[3] * target.height} vector-effect="non-scaling-stroke"/>
  </svg>` : null;
}
function ComparisonFrame({label, target, frame, region, onFrame, onError}) {
  const video = useRef(null), editing = useRef(false);
  const [frameInput, setFrameInput] = useState(String(frame));
  const seek = element => {if (element && element.readyState >= 1) {element.pause(); element.currentTime = frameTime(frame, target);}};
  useEffect(() => {seek(video.current);}, [frame, target.target, target.sha256, target.canvas_file_sha256]);
  useEffect(() => {
    if (!editing.current) setFrameInput(String(frame));
  }, [frame, target.target, target.sha256, target.canvas_file_sha256, target.f0, target.f1]);
  const commitFrame = value => {
    if (!editing.current) return; // Enter followed by blur commits only once.
    editing.current = false;
    if (integer(value) && Number(value) >= target.f0 && Number(value) < target.f1) {
      setFrameInput(String(Number(value)));
      if (Number(value) !== frame) onFrame((Number(value) - target.f0) / target.fps);
    } else setFrameInput(String(frame));
  };
  return html`<div className="qc-comparison-lane">
    <h4>${label}</h4><p className="qc-caption">${target.target} · [${target.f0}, ${target.f1}) · ${target.fps} fps</p>
    <div className="qc-video-stage">
      <video ref=${video} src=${pinnedMediaUrl(target)} muted playsInline preload="metadata" aria-label=${`${label} comparison preview`}
        onLoadedMetadata=${event => seek(event.currentTarget)} onPlay=${event => event.currentTarget.pause()}
        onError=${() => onError(`${label} video could not load. Refresh QC to check its pin.`)}/>
      <${RegionOverlay} target=${target} region=${region}/>
    </div>
    <label>${label} frame<input type="number" min=${target.f0} max=${target.f1 - 1} step="1" value=${frameInput}
      title="Press Enter or leave this field to seek to the frame."
      onFocus=${() => {editing.current = true;}}
      onChange=${event => {editing.current = true; setFrameInput(event.target.value);}}
      onBlur=${event => commitFrame(event.currentTarget.value)}
      onKeyDown=${event => {if (event.key === 'Enter') {event.preventDefault(); commitFrame(event.currentTarget.value);}}}/></label>
  </div>`;
}
export function ComparisonPlayer({before, after, onError}) {
  const [seconds, setSeconds] = useState(0), position = comparisonFrames(before, after, seconds);
  // Explicit paused-frame controls avoid decoder playback drift and make each
  // side's actual selected frame visible, even with different frame rates.
  return html`<div className="qc-comparison-player">
    <div className="qc-comparison-lanes">
      <${ComparisonFrame} label="Original" target=${before} frame=${position.before} region=${before.region} onFrame=${setSeconds} onError=${onError}/>
      <${ComparisonFrame} label="Newer" target=${after} frame=${position.after} region=${before.region} onFrame=${setSeconds} onError=${onError}/>
    </div>
    <label>Offset from each selected window start · ${position.offset.toFixed(3)} s<input type="range" min="0" max=${position.max} step="any" value=${position.offset} disabled=${position.max === 0} aria-label="Comparison window offset"
      onChange=${event => setSeconds(Number(event.target.value))}/></label>
    <div className="qc-actions"><${Button} variant="text" size="sm" disabled=${position.offset <= 0} onClick=${() => setSeconds(Math.max(0, position.offset - position.step))}>Previous pair<//>
      <${Button} variant="text" size="sm" disabled=${position.offset >= position.max} onClick=${() => setSeconds(Math.min(position.max, position.offset + position.step))}>Next pair<//></div>
    <p className="qc-caption">Paused frame comparison. Both sides advance by elapsed offset from their selected starts. The shorter window holds its last frame. This does not track matching content or establish temporal alignment.</p>
  </div>`;
}
function FindingComparison({session, locked}) {
  const {comparison, data} = session.state;
  const targets = data?.targets || [], items = data?.items || [], {item, after} = comparison;
  const recheckable = ['open', 'needs_recheck'].includes(item.status);
  const choices = newerTargets(item, targets), original = targets.find(t => sameTargetPin(t, item.anchor));
  let problem = '', body;
  try {body = comparisonBody(comparison, items, targets);} catch (error) {problem = error.message;}
  const sameClock = after && after.fps === item.anchor.fps && after.frames === item.anchor.frames;
  return html`<section className="qc-comparison" aria-label=${`Compare finding ${item.id}`}>
    <div className="qc-finding-top"><h3>Compare finding ${item.id}</h3><button className="text-action" onClick=${session.closeComparison}>Close comparison</button></div>
    <p className="qc-caption">Finding revision ${item.revision} · ${item.lane === 'audit' ? 'Audit · private' : 'Shared'} · Original ${item.anchor.target} [${item.anchor.f0}, ${item.anchor.f1})</p>
    <dl><dt>Expected</dt><dd>${item.expected}</dd><dt>Observed</dt><dd>${item.observed}</dd></dl>
    <fieldset disabled=${locked} className="qc-fields"><legend>Newer frame window</legend>
      <label>Newer version of ${item.anchor.output}<select aria-label="Comparison newer version" value=${after?.target || ''} onChange=${event => session.comparisonTarget(event.target.value)}>
        ${!choices.some(t => t.target === after?.target) && html`<option value=${after?.target || ''}>${after ? 'Selected version unavailable' : 'No newer render available'}</option>`}
        ${choices.map(t => html`<option key=${t.target} value=${t.target}>${t.target} · ${t.fps} fps · ${t.frames} frames</option>`)}</select></label>
      ${after && html`<div className="qc-field-pair"><label>Newer start frame<input type="number" min="0" max=${after.frames - 1} step="1" value=${comparison.f0} onChange=${event => session.comparisonWindow({f0:event.target.value})}/></label>
        <label>Newer end (exclusive)<input type="number" min="1" max=${after.frames} step="1" value=${comparison.f1} onChange=${event => session.comparisonWindow({f1:event.target.value})}/></label></div>`}
    </fieldset>
    <p className="qc-caption">${sameClock ? 'Frame rate and frame count match, so the original numbers are prefilled for you to review.' : 'Frame rate or frame count differs. Choose both newer frame boundaries explicitly; frame numbers are not converted.'} Edits may move content even when the clocks match.</p>
    ${problem && html`<p className="qc-validation" role="status">${problem}</p><button className="text-action" disabled=${locked} onClick=${session.reloadComparison}>Reload comparison</button>`}
    ${body && original && html`<${ComparisonPlayer} key=${JSON.stringify([item.id, item.revision, item.anchor.sha256, after.sha256, after.canvas_file_sha256, body.f0, body.f1])}
      before=${{...original, ...item.anchor, url:original.url}} after=${{...after, f0:body.f0, f1:body.f1}} onError=${session.error}/>`}
    ${item.anchor.region && html`<p className="qc-caption">The original normalized region is shown unchanged on both versions. It is not tracked and may cover different content after edits or resizing.</p>`}
    ${!recheckable && html`<p className="qc-validation">This finding is ${item.status}. Reopen it with a human review action before requesting another model recheck.</p>`}
    <label className="qc-confirm"><input type="checkbox" checked=${comparison.confirmed} disabled=${locked || !!problem || !recheckable} onChange=${event => session.confirmComparison(event.target.checked)}/>
      I confirm the newer window ${body ? `[${body.f0}, ${body.f1}) on ${after.target}` : 'after choosing valid frames'} for this recheck.</label>
    <${Button} variant="secondary" size="sm" disabled=${locked || !!problem || !recheckable || !comparison.confirmed} onClick=${session.recheck}>Recheck with fresh context<//>
    <p className="qc-caption">Starts a model job in the QC queue using this frozen finding and the selected windows. Opening this comparison does not run a model. Use the existing QC Stop / Resume controls. Results cover sampled frames only; resolving a finding remains a human action.</p>
  </section>`;
}
export function RecheckResults({item, rechecks, targets}) {
  const records = rechecks.filter(r => r.item_id === item.id).slice().reverse();
  const labels = {persists:'Issue observed', not_observed:'Not observed in sampled frames', inconclusive:'Inconclusive'};
  return records.length ? html`<section className="qc-rechecks" aria-label=${`Recheck results for ${item.id}`}><h4>Model rechecks (${records.length})</h4>
    ${records.map(record => {
      const stale = recheckStaleReason(record, item, targets), result = record.result;
      const created = record.created_at ? new Date(typeof record.created_at === 'number' ? record.created_at * 1000 : record.created_at).toLocaleString() : 'Time not reported';
      return html`<article className="qc-recheck" key=${record.id}>
        ${stale && html`<p className="qc-validation" role="status"><strong>Outdated recheck</strong> · ${stale}</p>`}
        <strong className="qc-recheck-assessment">${result ? labels[result.assessment] || 'Assessment not reported' : 'Recheck pending'}</strong>
        <p className="qc-caption">${record.model || 'Model not reported'} · ${record.provider || 'Provider not reported'} · ${created} · Finding revision ${record.item_revision}</p>
        <p className="qc-caption">Original ${record.before?.target} [${record.before?.f0}, ${record.before?.f1}) → Newer ${record.after?.target} [${record.after?.f0}, ${record.after?.f1})</p>
        ${record.job && html`<p className="qc-caption">QC job ${record.job.id} · ${record.job.status}${record.job.error ? ` · ${record.job.error}` : ''}</p>`}
        ${result ? html`<p className="qc-recheck-summary">${result.summary}</p><p className="qc-caption"><strong>Unqualified visual review</strong> · Sampled frames only. Unsampled frames are not assessed. This result does not resolve the finding.</p>
          <details className="qc-evidence" open><summary>Actual sampled frame coverage</summary>
            ${!result.evidence?.length && html`<p className="qc-validation">No sampled frame evidence reported.</p>`}
            ${(result.evidence || []).map((evidence, index) => {
              const side = sameTargetPin(evidence, record.before) ? 'Original' : sameTargetPin(evidence, record.after) ? 'Newer' : 'Unmatched pin';
              const frames = Array.isArray(evidence.frames) ? evidence.frames.filter(Number.isSafeInteger) : [];
              return html`<div className="qc-evidence-entry" key=${index}><p><strong>${side}</strong> · ${evidence.target}</p>
                <p>Frames actually sampled: ${frames.length ? frames.join(', ') : 'None reported'}</p>
                <p className="qc-caption">Coverage: ${evidence.coverage || 'Not reported'}</p>
                ${validRegion(evidence.region) && html`<p className="qc-caption">Region inspected (normalized): [${evidence.region.join(', ')}]</p>`}
                <details><summary>Evidence pins</summary><p className="qc-caption">Media SHA-256: ${evidence.sha256 || 'Not reported'}<br/>Canvas SHA-256: ${evidence.canvas_file_sha256 || 'Not reported'}</p></details>
              </div>`;
            })}
          </details>` : html`<p className="qc-caption">${record.job?.status === 'failed' ? 'The job failed; no assessment is available.' : 'Waiting for a model result. Queueing or stopping a job is not an assessment.'}</p>`}
        <details><summary>Frozen finding & recheck record</summary><p className="qc-caption">Recheck ${record.id} · ${record.status}${record.completed_at ? ` · Completed ${stamp(record.completed_at)}` : ''}</p>
          <dl><dt>Expected</dt><dd>${record.item?.expected || 'Not reported'}</dd><dt>Observed</dt><dd>${record.item?.observed || 'Not reported'}</dd></dl>
          ${[record.before, record.after].filter(Boolean).map(pin => html`<p key=${pin.target} className="qc-caption">${pin.target}<br/>Media SHA-256: ${pin.sha256}<br/>Canvas SHA-256: ${pin.canvas_file_sha256}</p>`)}
        </details>
      </article>`;
    })}
  </section>` : null;
}

function ReviewPlayer({target, draft, setDraft, visible, onError, items = []}) {
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
      <video ref=${video} src=${pinnedMediaUrl(target)} controls=${!drawing} playsInline preload="metadata" aria-label=${`QC preview ${target.target}`}
        onLoadedMetadata=${() => seek(draft.f0)} onTimeUpdate=${event => setCurrentFrame(frameAt(event.currentTarget.currentTime, target) ?? 0)}
        onError=${() => onError('This rendered video could not load. Refresh QC and try the target again.')} />
      ${validRegion(region) && html`<svg className="qc-region-overlay" viewBox=${`0 0 ${target.width} ${target.height}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        <rect x=${region[0] * target.width} y=${region[1] * target.height} width=${region[2] * target.width} height=${region[3] * target.height} vector-effect="non-scaling-stroke"/>
      </svg>`}
    </div>
    <p className="qc-caption">${target.width} × ${target.height} · ${target.fps} fps · ${target.frames} frames · Pinned version</p>
    <div className="qc-marker-timeline" aria-label="Findings on this pinned timeline">
      ${findingMarkers(items, target).map(({item, left, width}) => html`<div className="qc-marker-row" key=${item.id}>
        <button className="qc-timeline-marker" style=${{left:`${left}%`, width:`${width}%`}} title=${`${item.id}: ${item.observed}`}
          aria-label=${`Review finding ${item.id} at frames [${item.anchor.f0}, ${item.anchor.f1})`}
          onClick=${() => {video.current?.pause(); seek(item.anchor.f0); setDraft({f0:String(item.anchor.f0), f1:String(item.anchor.f1), selectionMode:item.anchor.f1 === item.anchor.f0 + 1 ? 'frame' : 'range', region:item.anchor.region || null, seek:(draft.seek || 0) + 1});}}><span className="visually-hidden">${item.id}</span></button>
      </div>`)}
      ${findingMarkers(items, target).length > 0 && html`<p className="qc-caption">Finding markers match this exact render and canvas pin. Select a marker to review its original frames and region.</p>`}
    </div>
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
  useEffect(() => {
    if (visible && session.state.comparisonOpen) feed.current?.querySelector('.qc-comparison')?.scrollIntoView({block:'nearest'});
  }, [visible, session.state.comparisonOpen, session.state.comparison?.item.id]);
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
        ${target && !changed && html`<fieldset className="qc-review-controls" disabled=${locked}><${ReviewPlayer} key=${`${target.target}:${target.sha256}:${target.canvas_file_sha256}`} target=${target} draft=${draft} setDraft=${setDraft} visible=${visible && reviewOpen} onError=${session.error} items=${items}/>
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
        ${session.state.comparisonOpen && session.state.comparison && html`<${FindingComparison} session=${session} locked=${locked}/>`}
        <section className="qc-findings" aria-label="QC findings"><h3>Findings (${items.length})</h3>
          <p className="qc-caption">Resolved means human reviewed. Findings and resolutions are not regulatory acceptance.</p>
          ${!items.length && html`<p className="qc-caption">No findings recorded yet.</p>`}
          ${items.map(item => html`<article className="qc-finding" key=${item.id}>
            <div className="qc-finding-top"><label className="qc-item-select"><input type="checkbox" checked=${selected.includes(item.id)} disabled=${locked || !handoffItems([item], [item.id]).length} onChange=${event => session.select(item.id, event.target.checked)} aria-label=${`Select finding ${item.id} for editor`}/><strong>${item.category} · ${item.priority}</strong></label><span>${item.lane === 'audit' ? 'Audit · private' : 'Shared'}</span></div>
            <p className=${`qc-item-status ${item.status === 'needs_recheck' ? 'needs-recheck' : ''}`}>${item.status === 'needs_recheck' ? 'Needs recheck · a newer version requires review' : item.status === 'resolved' ? 'Resolved · human reviewed' : item.status === 'dismissed' ? 'Dismissed by human' : item.status === 'open' ? 'Open' : item.status}</p>
            <p className="qc-caption">${item.anchor?.target} · [${item.anchor?.f0}, ${item.anchor?.f1})${item.anchor?.region ? ` · region [${item.anchor.region.map(n => Number(n).toFixed(3)).join(', ')}]` : ''}</p>
            <dl><dt>Expected</dt><dd>${item.expected}</dd><dt>Observed</dt><dd>${item.observed}</dd></dl>
            <button className="text-action" disabled=${locked || !targets.some(t => sameTargetPin(t, item.anchor))} onClick=${() => {setDraft({...selectTarget(targets.find(t => sameTargetPin(t, item.anchor))), f0:String(item.anchor.f0), f1:String(item.anchor.f1), selectionMode:item.anchor.f1 === item.anchor.f0 + 1 ? 'frame' : 'range', region:item.anchor.region || null, seek:(draft.seek || 0) + 1}); feed.current?.scrollTo({top:0});}}>Review pinned frames</button>
            <button className="text-action qc-compare-action" disabled=${locked} onClick=${() => {session.openComparison(item.id);}}>Compare & recheck</button>
            <${RecheckResults} item=${item} rechecks=${data?.rechecks || []} targets=${targets}/>
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
