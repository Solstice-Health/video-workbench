import htm from './vendor/htm.mjs';
import {WORKSPACE_FILE_MIME, isActiveJob, pointInCanvas, fitPlacement, scalePlacement, movePlacement,
  boxFromPoints, imageSize, workspaceDrop, inputLimit, draftDefaults, requestBody, paintStrokes, safeDraft,
  videoProviders, videoProvider, selectVideoProvider, videoOptions, videoSettingsError} from './generation-geometry.js';
const {createElement, useState, useEffect, useRef} = window.React;
const html = htm.bind(createElement);
const {Button, Icon, Tooltip, MessageBar, SolsticeMark} = window.Eeva;
const drafts = new Map();
const saveTimers = new Map();
const busyDrafts = new Set(), subscribers = new Map();
const labels = {create: 'Create', edit: 'Edit', remove_background: 'Remove background', inpaint: 'Inpaint', outpaint: 'Outpaint',
  text: 'From a prompt', frames: 'From frames', reference: 'From references', extend: 'Extend'};
const instructions = {
  create: 'Describe an image, or add references to guide its appearance.',
  edit: 'Describe your changes. References and optional markup guide a newly generated image.',
  remove_background: 'Name the subject to keep. Draw a region to narrow the selection, or use the whole image.',
  inpaint: 'Paint the pixels to replace. Every unmarked source pixel is retained exactly.',
  outpaint: 'Move and scale the image inside the canvas. Describe what should fill the surrounding space.',
  text: 'Describe the scene, motion and camera. Your clip is saved to this workspace.',
  frames: 'Add the first frame, then an optional last frame. Describe the motion between them.',
  reference: 'Add up to three image references and describe the scene and motion.',
  extend: 'Add one video and describe how it should continue.',
};
function readDraft(key, kind) {
  if (drafts.has(key)) return drafts.get(key);
  try {
    const value = JSON.parse(window.sessionStorage.getItem(`vwb-studio:${key}`));
    if (value && Array.isArray(value.sources) && Array.isArray(value.strokes)) return {...draftDefaults(kind),
      ...(kind === 'video' && value.provider === 'openrouter' ? {seconds: '', resolution: '', aspect: ''} : {}), ...value};
  } catch { /* A restricted browser still keeps drafts while this page is open. */ }
  return draftDefaults(kind);
}
function remember(key, value) {
  if (drafts.size > 30 && !drafts.has(key)) drafts.delete(drafts.keys().next().value);
  drafts.set(key, value);
  for (const listener of subscribers.get(key) || []) listener({draft: value});
  clearTimeout(saveTimers.get(key));
  saveTimers.set(key, setTimeout(() => {
    try {window.sessionStorage.setItem(`vwb-studio:${key}`, JSON.stringify(safeDraft(drafts.get(key) || value)));} catch { /* Memory draft remains available. */ }
    saveTimers.delete(key);
  }, 250));
}
function notifyWorkspace(requestId) {
  window.dispatchEvent(new CustomEvent('vwb-workspace-changed', {detail: {requestId}}));
}
function IconAction({label, name, ...props}) {
  return html`<${Tooltip} content=${label}><${Button} variant="text" iconOnly aria-label=${label} ...${props}><${Icon} name=${name} size=${16}/><//><//>`;
}
function Field({label, children, hint, className = ''}) {
  return html`<label className=${`studio-field ${className}`}><span>${label}</span>${children}${hint && html`<small>${hint}</small>`}</label>`;
}
function VideoOption({label, value, values, disabled, onChange, suffix = ''}) {
  const selected = value ?? '', unsupported = selected !== '' && !values.some(item => String(item) === String(selected));
  return html`<${Field} label=${label}><select aria-label=${label} value=${selected} disabled=${disabled} onChange=${onChange}>
    <option value="">Provider default</option>
    ${unsupported && html`<option value=${selected}>${selected}${suffix} · Not advertised</option>`}
    ${values.map(item => html`<option key=${item} value=${item}>${item}${suffix}</option>`)}</select><//>`;
}
function WaitingField({kind}) {
  const canvas = useRef(null);
  useEffect(() => {
    const element = canvas.current, context = element.getContext('2d');
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0, last = 0, width = 0, height = 0;
    const color = getComputedStyle(element).getPropertyValue('--ds-text-primary').trim();
    const draw = now => {
      if (now - last > 32 || motion.matches) {
        last = now; context.clearRect(0, 0, width, height);
        const t = motion.matches ? 0 : now / 1300;
        for (let row = 0; row < 13; row++) for (let col = 0; col < 29; col++) {
          const nx = col / 28, ny = row / 12, wave = Math.sin(nx * 8 + t) * Math.cos(ny * 5 - t * .7);
          const distance = Math.hypot(nx - .5, ny - .5);
          context.globalAlpha = Math.max(0, (.22 - distance * .22) + wave * .035);
          context.fillStyle = color; context.beginPath();
          context.arc(width * (.08 + nx * .84), height * (.22 + ny * .56) + wave * 12, 1.1 + (wave + 1) * .35, 0, Math.PI * 2); context.fill();
        }
      }
      if (!motion.matches) frame = requestAnimationFrame(draw);
    };
    const resize = () => {
      const rect = element.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width; height = rect.height; element.width = width * dpr; element.height = height * dpr;
      context.setTransform(dpr, 0, 0, dpr, 0, 0); cancelAnimationFrame(frame); draw(performance.now());
    };
    const observer = new ResizeObserver(resize); observer.observe(element); motion.addEventListener('change', resize);
    return () => {observer.disconnect(); motion.removeEventListener('change', resize); cancelAnimationFrame(frame);};
  }, []);
  return html`<div className="studio-waiting"><canvas ref=${canvas} aria-hidden="true"/><div className="studio-waiting-center">
    <span className="studio-morph-mark"><${SolsticeMark} size=${32} spinning/></span><span>Creating your ${kind}</span><small>You can switch tabs while this runs.</small></div></div>`;
}
function SourceCanvas({draft, setDraft, size, disabled, onError}) {
  const surface = useRef(null), mask = useRef(null), gesture = useRef(null);
  const source = draft.sources[0], operation = draft.operation;
  const drawing = operation === 'inpaint' || operation === 'edit' && draft.markup;
  const movable = operation === 'outpaint', selecting = operation === 'remove_background';
  const dimensions = drawing || selecting ? [source.width, source.height] : size;
  const [width, height] = dimensions;
  const place = movable ? draft.place || fitPlacement(source, size) : [0, 0, width, height];
  useEffect(() => {
    if (!mask.current || !drawing) return;
    const context = mask.current.getContext('2d'); context.clearRect(0, 0, width, height);
    let live = true;
    const render = () => paintStrokes(context, draft.strokes, operation === 'inpaint' ? '#A85600' : '#EF8006');
    if (draft.savedMask?.url && operation === 'inpaint') {
      const img = new Image(); img.onload = () => {if (live) {context.drawImage(img, 0, 0); context.globalCompositeOperation = 'source-in'; context.fillStyle = '#A85600'; context.fillRect(0, 0, width, height); context.globalCompositeOperation = 'source-over'; render();}};
      img.onerror = () => {if (live) onError('The saved mask could not be previewed. Clear it and draw a new mask.');}; img.src = draft.savedMask.url;
    } else render();
    return () => {live = false;};
  }, [draft.strokes, draft.savedMask?.url, drawing, width, height, operation]);
  const point = event => pointInCanvas(event.clientX, event.clientY, surface.current.getBoundingClientRect(), width, height);
  const down = event => {
    if (disabled || event.button !== 0 || !(drawing || movable || selecting)) return;
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId);
    const p = point(event); gesture.current = {point: p, place, pointerId: event.pointerId};
    if (drawing) {
      if (draft.strokes.length >= 1000) {gesture.current = null; onError('The markup is full. Clear a few strokes before drawing more.'); return;}
      setDraft(d => ({...d, strokes: [...d.strokes, {width: d.brush, erase: d.tool === 'erase', points: [p]}]}));
    } else if (selecting) setDraft(d => ({...d, box: boxFromPoints(p, p, dimensions)}));
  };
  const move = event => {
    if (!gesture.current || gesture.current.pointerId !== event.pointerId) return;
    const p = point(event), g = gesture.current;
    if (drawing) setDraft(d => {
      const strokes = d.strokes.slice(), last = strokes.at(-1);
      if (!last || last.points.length >= 10000) return d;
      const previous = last.points.at(-1);
      if (Math.hypot(previous[0] - p[0], previous[1] - p[1]) < 1) return d;
      strokes[strokes.length - 1] = {...last, points: [...last.points, p]}; return {...d, strokes};
    });
    else if (movable) setDraft(d => ({...d, place: movePlacement(g.place, p[0] - g.point[0], p[1] - g.point[1], size)}));
    else if (selecting) setDraft(d => ({...d, box: boxFromPoints(g.point, p, dimensions)}));
  };
  const finish = () => {gesture.current = null;};
  const key = event => {
    if (!movable || disabled) return;
    const delta = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
    if (!delta) return; event.preventDefault(); const n = event.shiftKey ? 10 : 1;
    setDraft(d => ({...d, place: movePlacement(place, delta[0] * n, delta[1] * n, size)}));
  };
  return html`<div className="studio-artboard-wrap"><div className="studio-artboard" ref=${surface}
    style=${{aspectRatio: `${width}/${height}`, '--studio-ratio': width / height}}
    tabIndex=${movable ? 0 : undefined} role=${movable ? 'group' : undefined}
    aria-label=${movable ? 'Image placement. Use arrow keys to move by one pixel, or Shift and arrows for ten.' : undefined}
    onKeyDown=${key} onPointerDown=${down} onPointerMove=${move} onPointerUp=${finish} onPointerCancel=${finish}
    onLostPointerCapture=${finish} data-tool=${drawing ? draft.tool : movable ? 'move' : selecting ? 'region' : 'view'}>
    <img src=${source.url} alt="Source image" draggable="false" style=${{left: `${place[0] / width * 100}%`, top: `${place[1] / height * 100}%`,
      width: `${place[2] / width * 100}%`, height: `${place[3] / height * 100}%`, objectFit: movable || drawing || selecting ? 'fill' : 'contain'}}/>
    ${drawing && html`<canvas ref=${mask} width=${width} height=${height} className=${`studio-mask ${operation === 'edit' ? 'markup' : ''}`} aria-hidden="true"/>`}
    ${selecting && draft.box && html`<div className="studio-subject-region" style=${{left: `${draft.box[0] / width * 100}%`, top: `${draft.box[1] / height * 100}%`, width: `${draft.box[2] / width * 100}%`, height: `${draft.box[3] / height * 100}%`}}/>`}
    ${movable && html`<div className="studio-placement-edge" style=${{left: `${place[0] / width * 100}%`, top: `${place[1] / height * 100}%`, width: `${place[2] / width * 100}%`, height: `${place[3] / height * 100}%`}}/>`}
  </div><div className="studio-dimension-label">${width} × ${height} px${drawing ? operation === 'inpaint' ? ' · Marked pixels are replaced' : ' · Markup is a reference' : movable ? ' · Drag image to position' : ''}</div></div>`;
}
async function imageDimensions(url) {
  return new Promise((resolve, reject) => {
    const img = new Image(); img.onload = () => {
      if (img.naturalWidth * img.naturalHeight > 16777216 || Math.max(img.naturalWidth, img.naturalHeight) > 8192) reject(new Error('Choose an image up to 16 megapixels and 8192 pixels per edge.'));
      else resolve({width: img.naturalWidth, height: img.naturalHeight});
    };
    img.onerror = () => reject(new Error('This image could not be previewed. Choose a PNG, JPEG or WebP image.')); img.src = url;
  });
}
async function rasterize(draft, annotated = false) {
  const source = draft.sources[0];
  if (!source?.width || !source?.height || source.width * source.height > 32000000) throw new Error('Choose a source smaller than 32 megapixels for a drawn edit.');
  const canvas = document.createElement('canvas'); canvas.width = source.width; canvas.height = source.height;
  const ctx = canvas.getContext('2d');
  if (annotated) {
    const img = new Image(); img.src = source.url; await img.decode(); ctx.drawImage(img, 0, 0);
    // Erasing markup must not erase the underlying reference image.
    const overlay = document.createElement('canvas'); overlay.width = canvas.width; overlay.height = canvas.height;
    paintStrokes(overlay.getContext('2d'), draft.strokes, '#EF8006'); ctx.drawImage(overlay, 0, 0);
  } else {
    if (draft.savedMask?.url) {const img = new Image(); img.src = draft.savedMask.url; await img.decode(); ctx.drawImage(img, 0, 0);}
    paintStrokes(ctx, draft.strokes);
    const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let selected = false; for (let i = 3; i < data.length; i += 4) if (data[i] > 127) {selected = true; break;}
    if (!selected) throw new Error('The mask is empty. Paint the pixels you want to replace.');
  }
  return new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('The drawing could not be saved. Try again.')), 'image/png'));
}

export function GenerationStudio(props) {
  return html`<${StudioWorkspace} key=${`${props.requestId}:${props.kind}`} ...${props}/>`;
}
function StudioWorkspace({kind, api, requestId, onOpen}) {
  const key = `${requestId}:${kind}`, base = `/api/requests/${encodeURIComponent(requestId)}/studio`;
  const [draft, updateDraft] = useState(() => readDraft(key, kind));
  const draftRef = useRef(draft), alive = useRef(true), uploadInput = useRef(null), dragDepth = useRef(0), announced = useRef(new Set());
  const [capabilities, setCapabilities] = useState(null), [jobs, setJobs] = useState([]), [job, setJob] = useState(null);
  const [error, setError] = useState(''), [loading, setLoading] = useState(true), [busy, updateBusy] = useState(() => busyDrafts.has(key)), [dragging, setDragging] = useState(false);
  const [workspacePath, setWorkspacePath] = useState(''), [showPath, setShowPath] = useState(false), [revision, setRevision] = useState(0);
  const [catalog, setCatalog] = useState({query: '', status: 'idle', items: []}), [catalogRetry, setCatalogRetry] = useState(0);
  const setDraft = fn => {
    const previous = drafts.get(key) || draftRef.current, next = typeof fn === 'function' ? fn(previous) : {...previous, ...fn};
    draftRef.current = next; remember(key, next); if (alive.current) updateDraft(next);
  };
  const setBusy = value => {
    if (value) busyDrafts.add(key); else busyDrafts.delete(key);
    for (const listener of subscribers.get(key) || []) listener({busy: value});
    if (alive.current) updateBusy(value);
  };
  useEffect(() => {
    const listener = update => {
      if (update.draft) {draftRef.current = update.draft; updateDraft(update.draft);}
      if ('busy' in update) updateBusy(update.busy);
    };
    const listeners = subscribers.get(key) || new Set(); listeners.add(listener); subscribers.set(key, listeners);
    return () => {listeners.delete(listener); if (!listeners.size) subscribers.delete(key);};
  }, [key]);
  const cap = capabilities?.[kind], active = isActiveJob(job), locked = active || busy;
  const provider = kind === 'video' ? videoProvider(cap, draft.provider || 'google') : null;
  const openrouter = kind === 'video' && draft.provider === 'openrouter';
  const modelQuery = (draft.model || '').trim();
  const currentCatalog = catalog.query === modelQuery ? catalog : {status: 'loading', items: []};
  const selectedModel = openrouter && currentCatalog.status === 'ready' ? currentCatalog.items.find(model => model.id === modelQuery) : null;
  const options = videoOptions(provider, selectedModel), operations = kind === 'video' ? provider?.operations || [] : cap?.operations || [];
  const videoError = kind === 'video' ? videoSettingsError(draft, provider, selectedModel) : '';
  const localBackground = kind === 'image' && draft.operation === 'remove_background' && draft.background_method === 'matte';
  const localJob = job?.kind === 'image' && job.operation === 'remove_background' && job.input?.background_method === 'matte';
  const providerAvailable = kind === 'video' ? provider?.available : cap?.available;
  const canRun = localBackground ? cap?.background_methods?.some(method => method.id === 'matte' && method.available) : providerAvailable;
  const videoSize = draft.resolution === '720p' ? [720, 1280] : [1080, 1920];
  const size = kind === 'image' ? imageSize(draft.size, cap?.size_constraints) || [1024, 1024]
    : draft.aspect === '16:9' ? [...videoSize].reverse() : videoSize;
  const source = draft.sources[0];
  const output = job?.status === 'completed' ? job.result?.artifacts?.find(a => kind === 'image' ? a.mime?.startsWith('image/') : a.mime?.startsWith('video/')) : null;
  const resultVisible = draft.showResult && output;
  const acceptsVideo = kind === 'video' && ['edit', 'extend'].includes(draft.operation);
  const maxInputs = inputLimit(kind, draft.operation);
  const loadMetadata = async path => {
    const meta = await api(`/api/requests/${encodeURIComponent(requestId)}/workspace/file?path=${encodeURIComponent(path)}`);
    if (!(meta.mime?.startsWith('image/') || meta.mime?.startsWith('video/'))) throw new Error('Choose an image or video file from the workspace.');
    if (meta.mime.startsWith('image/') && !['image/png', 'image/jpeg', 'image/webp'].includes(meta.mime)) throw new Error('Choose a PNG, JPEG or WebP source image.');
    return {...meta, kind: meta.mime.startsWith('video/') ? 'video' : 'image', ...(meta.mime.startsWith('image/') ? await imageDimensions(meta.url) : {})};
  };
  useEffect(() => {
    if (!draft.jobId || job?.id === draft.jobId || loading) return;
    let live = true;
    api(`${base}/jobs/${encodeURIComponent(draft.jobId)}`).then(latest => {if (live) {setJob(latest); setJobs(list => [latest, ...list.filter(j => j.id !== latest.id)]);}})
      .catch(e => {if (live) setError(e.message);});
    return () => {live = false;};
  }, [draft.jobId, loading]);
  useEffect(() => {
    alive.current = true; let live = true; setLoading(true); setError('');
    Promise.all([api(`${base}/capabilities`), api(`${base}/jobs?kind=${kind}`)]).then(async ([caps, history]) => {
      if (!live) return;
      setCapabilities(caps); setJobs(history.jobs || []);
      const list = history.jobs || [], recovered = list.find(j => j.input?.idempotency_key === draftRef.current.pendingSubmission?.payload?.idempotency_key);
      const current = recovered || list.find(isActiveJob) || list.find(j => j.id === draftRef.current.jobId) || list[0];
      if (current) {setJob(current); setDraft(d => ({...d, jobId: current.id, pendingSubmission: recovered ? null : d.pendingSubmission,
        showResult: current.status === 'completed' ? d.showResult ?? true : false}));}
      const settings = caps[kind];
      if (settings) setDraft(d => {
        if (kind === 'video') {
          if (!d.model && !d.videoPreferences && settings.provider && settings.provider !== (d.provider || 'google')) return selectVideoProvider(d, settings.provider, settings);
          const selected = videoProvider(settings, d.provider || 'google');
          // A persisted custom path and operation survive refresh, missing keys,
          // and catalog failures. Never silently route a paid request to Google.
          return {...d, provider: d.provider || 'google', model: d.model || selected?.default_model || (selected?.models?.find(m => m.default) || selected?.models?.[0])?.id || ''};
        }
        return {...d, model: settings.models.some(m => m.id === d.model) ? d.model : (settings.models.find(m => m.default) || settings.models[0])?.id || '',
          operation: settings.operations.includes(d.operation) ? d.operation : settings.operations[0]};
      });
      // URLs are re-resolved after reload; only durable paths are stored.
      const restored = await Promise.all(draftRef.current.sources.map(async s => s.url ? s : {...s, ...await loadMetadata(s.path)}));
      const savedMask = draftRef.current.savedMask;
      const restoredMask = savedMask && !savedMask.url ? {...savedMask, ...await loadMetadata(savedMask.path)} : savedMask;
      if (live) setDraft(d => ({...d, sources: restored, savedMask: restoredMask}));
    }).catch(e => {if (live) setError(e.message);}).finally(() => {if (live) setLoading(false);});
    return () => {live = false; alive.current = false;};
  }, [requestId, kind, revision]);
  useEffect(() => {
    if (!openrouter || loading || !provider?.available) return;
    let live = true;
    const controller = new AbortController();
    setCatalog({query: modelQuery, status: 'loading', items: []});
    const timer = setTimeout(async () => {
      try {
        const result = await api(`${base}/video-models?query=${encodeURIComponent(modelQuery)}&limit=50`, {signal: controller.signal});
        if (!live) return;
        if (!Array.isArray(result.items)) throw new Error('The video catalog returned an invalid response.');
        setCatalog({query: modelQuery, status: 'ready', items: result.items.filter(row => row && typeof row.id === 'string'), total: result.total, next_offset: result.next_offset});
      } catch (e) {
        if (live) setCatalog({query: modelQuery, status: 'error', items: [], error: e.message});
      }
    }, 300);
    return () => {live = false; clearTimeout(timer); controller.abort();};
  }, [openrouter, modelQuery, base, loading, provider?.available, catalogRetry, revision]);
  useEffect(() => {
    if (!job || !isActiveJob(job)) return;
    let live = true, timer;
    const poll = async () => {
      try {
        const latest = await api(`${base}/jobs/${encodeURIComponent(job.id)}`);
        if (!live) return;
        setError(previous => previous.startsWith('Could not refresh this job:') ? '' : previous);
        setJob(latest); setJobs(list => [latest, ...list.filter(j => j.id !== latest.id)]);
        if (isActiveJob(latest)) timer = setTimeout(poll, 2000);
        else {
          setDraft(d => ({...d, showResult: latest.status === 'completed'}));
          if (latest.status === 'completed' && !announced.current.has(latest.id)) {announced.current.add(latest.id); notifyWorkspace(requestId);}
        }
      } catch (e) {if (live) {setError(`Could not refresh this job: ${e.message}`); timer = setTimeout(poll, 5000);}}
    };
    timer = setTimeout(poll, 1000); return () => {live = false; clearTimeout(timer);};
  }, [job?.id, isActiveJob(job)]);
  const changedOperation = operation => {
    if (kind === 'video') {setError(''); setDraft({operation, showResult: false}); return;}
    setError(''); setDraft(d => ({...d, operation, strokes: [], savedMask: null, box: null, place: d.sources[0]?.width ? fitPlacement(d.sources[0], size) : null,
      markup: false, showResult: false, sources: operation === 'text' ? [] : d.sources.slice(0, inputLimit(kind, operation)).filter(s => (kind === 'video' && ['edit', 'extend'].includes(operation)) === (s.kind === 'video'))}));
  };
  const addSources = async files => {
    if (locked) return;
    setError(''); setBusy(true);
    try {
      const available = maxInputs - draftRef.current.sources.length;
      if (files.length > available) throw new Error(available ? `You can add ${available} more ${available === 1 ? 'file' : 'files'} for this operation.` : 'Remove a source before adding another.');
      const added = [];
      for (const file of files) {
        const video = acceptsVideo;
        const capBytes = capabilities?.uploads?.[video ? 'video_max_bytes' : 'image_max_bytes'];
        if (capBytes && file.size > capBytes) throw new Error(`${file.name} exceeds the ${Math.round(capBytes / 1024 ** 2)} MB upload limit.`);
        const uploaded = await api(`${base}/uploads`, {method: 'POST', raw: true, headers: {'Content-Type': file.type || 'application/octet-stream',
          'X-Filename': encodeURIComponent(file.name), 'X-Upload-Kind': video ? 'video' : 'image'}, body: file});
        added.push({...uploaded, name: file.name}); notifyWorkspace(requestId);
      }
      setDraft(d => ({...d, sources: [...d.sources, ...added], strokes: [], savedMask: null, box: null, showResult: false,
        place: d.sources.length ? d.place : added[0]?.width ? fitPlacement(added[0], size) : null}));
    } catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const addWorkspace = async path => {
    if (locked) return;
    setBusy(true); setError('');
    try {
      if (draft.sources.length >= maxInputs) throw new Error('Remove a source before adding another.');
      const meta = await loadMetadata(path);
      if ((meta.kind === 'video') !== acceptsVideo) throw new Error(acceptsVideo ? 'Choose a video for this operation.' : 'Choose an image for this operation.');
      setDraft(d => ({...d, sources: [...d.sources, meta], strokes: [], savedMask: null, box: null, showResult: false,
        place: d.sources.length ? d.place : meta.width ? fitPlacement(meta, size) : null}));
      if (alive.current) {setShowPath(false); setWorkspacePath('');}
    } catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const drop = event => {
    event.preventDefault(); dragDepth.current = 0; setDragging(false);
    if (locked) return;
    const raw = event.dataTransfer.getData(WORKSPACE_FILE_MIME);
    if (raw) {try {addWorkspace(workspaceDrop(raw, requestId).path);} catch (e) {setError(e.message);}}
    else if (event.dataTransfer.files.length) addSources(Array.from(event.dataTransfer.files));
  };
  const submit = async event => {
    event.preventDefault(); if (locked || !canRun) return;
    setBusy(true); setError('');
    try {
      const d = draftRef.current;
      if (kind === 'video') {
        const invalid = videoSettingsError(d, provider, selectedModel);
        if (invalid) throw new Error(invalid);
      }
      // Validate the form before uploading any optional drawing.
      let payload = requestBody(d, kind, crypto.randomUUID(), d.operation === 'inpaint' ? {path: 'pending-mask'} : null, cap.size_constraints);
      const {idempotency_key, ...identity} = payload;
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify({identity, strokes: d.strokes, markup: d.markup,
        savedMask: d.savedMask?.sha256 || d.savedMask?.ref || d.savedMask?.path || null})));
      const signature = [...new Uint8Array(digest)].map(n => n.toString(16).padStart(2, '0')).join('');
      if (d.pendingSubmission?.signature === signature) payload = d.pendingSubmission.payload;
      else if (d.operation === 'inpaint' || d.operation === 'edit' && d.markup && d.strokes.length) {
        const annotation = d.operation === 'edit';
        if (annotation && payload.prompt.length > 19800) throw new Error('Shorten the prompt to 19,800 characters to leave room for the markup instructions.');
        const blob = await rasterize(d, annotation);
        if (annotation && payload.inputs.length >= 16) throw new Error('Leave one reference slot free for your markup.');
        const uploaded = await api(`${base}/uploads`, {method: 'POST', raw: true, headers: {'Content-Type': 'image/png',
          'X-Filename': annotation ? 'markup.png' : 'mask.png', 'X-Upload-Kind': annotation ? 'image' : 'mask'}, body: blob});
        notifyWorkspace(requestId);
        if (annotation) {payload.inputs.push({upload_id: uploaded.upload_id}); payload.prompt += '\nThe final reference shows colored markup over the first image. Follow these editing directions without reproducing the markup itself.';}
        else payload.mask = {upload_id: uploaded.upload_id};
      }
      setDraft({pendingSubmission: {signature, payload}});
      const created = await api(`${base}/jobs`, {method: 'POST', body: JSON.stringify(payload)});
      setDraft({jobId: created.id, showResult: false, pendingSubmission: null});
      if (alive.current) {setJob(created); setJobs(list => [created, ...list.filter(j => j.id !== created.id)]);}
    } catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const cancel = async () => {
    setBusy(true); setError('');
    try {const updated = await api(`${base}/jobs/${encodeURIComponent(job.id)}/cancel`, {method: 'POST', body: '{}'}); if (alive.current) {setJob(updated); setJobs(list => [updated, ...list.filter(j => j.id !== updated.id)]);}}
    catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const useOutput = async artifact => {
    setBusy(true); setError('');
    try {
      const meta = {...artifact, ...await loadMetadata(artifact.path)};
      if (alive.current) setDraft(d => ({...d, operation: 'edit', sources: [meta], strokes: [], savedMask: null, box: null, place: meta.width ? fitPlacement(meta, size) : null,
        markup: false, showResult: false, prompt: ''}));
    } catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const useBackgroundMask = async () => {
    const original = job?.reusable_inputs?.[0], backgroundMask = job?.result?.background_mask;
    if (!original?.path || !backgroundMask?.path) {setError('This result has no reusable source mask. Run background removal again.'); return;}
    setBusy(true); setError('');
    try {
      const [sourceMeta, maskMeta] = await Promise.all([loadMetadata(original.path), loadMetadata(backgroundMask.path)]);
      setDraft(d => ({...d, operation: 'inpaint', sources: [{...original, ...sourceMeta}], savedMask: {...backgroundMask, ...maskMeta},
        strokes: [], markup: false, box: null, place: null, showResult: false, prompt: '', pendingSubmission: null}));
    } catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const reuse = async selected => {
    const input = selected.input; if (!input) return;
    setBusy(true); setError('');
    try {
      const restored = await Promise.all((selected.reusable_inputs || input.inputs || []).map(async entry => {
        if (!entry.path) throw new Error('This saved input has no workspace path. Add its source from the workspace.');
        return {...entry, ...await loadMetadata(entry.path)};
      }));
      const savedMask = selected.reusable_mask ? {...selected.reusable_mask, ...await loadMetadata(selected.reusable_mask.path)} : null;
      if (alive.current) setDraft(d => ({...d, ...(kind === 'video' ? {provider: input.provider || 'google', seconds: input.seconds ?? '', resolution: input.resolution ?? '', aspect: input.aspect ?? '', generate_audio: input.generate_audio ?? '', seed: input.seed ?? ''} : {}),
        ...Object.fromEntries(['operation', 'background_method', 'prompt', 'model', 'quality', 'size', 'transparent', 'allow_text', 'subject', 'seconds', 'resolution', 'aspect', 'place', 'box'].filter(k => input[k] !== undefined).map(k => [k, input[k]])),
        background_method: input.background_method || (input.operation === 'remove_background' ? 'sunburst' : d.background_method),
        sources: restored, strokes: [], savedMask, markup: false, showResult: false}));
      if (input.mask && !savedMask) setError('Settings restored. Draw a new mask before generating another inpaint.');
    } catch (e) {if (alive.current) setError(e.message);} finally {setBusy(false);}
  };
  const updateSize = (index, value) => setDraft(d => {const next = [...d.size]; next[index] = value === '' ? '' : Number(value);
    const normalized = imageSize(next, cap?.size_constraints); return {...d, size: next, showResult: false,
      place: normalized && d.sources[0]?.width ? fitPlacement(d.sources[0], normalized) : d.place};});
  const status = error ? {tone: 'error', text: error} : !loading && !canRun && !active ? {tone: 'info', text: `The ${kind === 'image' ? 'image' : provider?.label || draft.provider || 'video'} provider is not configured. Add its gateway credentials to enable generation.${cap?.background_methods?.some(method => method.id === 'matte' && method.available) ? ' Local background removal remains available.' : ''}`}
    : active ? {tone: 'progress', text: job.status === 'queued' ? 'Queued. This job will start when a generation slot is free.' : `${localJob ? 'Preparing your cutout and background mask' : `Creating your ${kind}`}. You can keep working in another tab.`}
    : job?.status === 'failed' || job?.status === 'interrupted' ? {tone: 'error', text: job.error || 'This job stopped before an output was saved. Review the settings and try again.'}
    : job?.status === 'cancelled' ? {tone: 'info', text: localJob ? 'Background removal stopped.' : 'Stopped locally. A provider request may still finish and incur charges.'}
    : null;
  const drawing = kind === 'image' && (draft.operation === 'inpaint' || draft.operation === 'edit' && draft.markup);
  return html`<section className=${`generation-studio ${dragging ? 'is-dragging' : ''}`} aria-label=${`${kind === 'image' ? 'Image' : 'Video'} studio`}
    onDragEnter=${e => {if (e.dataTransfer.types.includes('Files') || e.dataTransfer.types.includes(WORKSPACE_FILE_MIME)) {e.preventDefault(); dragDepth.current++; setDragging(true);}}}
    onDragOver=${e => {if (e.dataTransfer.types.includes('Files') || e.dataTransfer.types.includes(WORKSPACE_FILE_MIME)) {e.preventDefault(); e.dataTransfer.dropEffect = locked ? 'none' : 'copy';}}}
    onDragLeave=${() => {dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false);}} onDrop=${drop}>
    <header className="studio-header"><div><span className="studio-eyebrow">${kind === 'image' ? 'Image studio' : 'Video studio'}</span><h2>${kind === 'image' ? 'Make room for an idea.' : 'Put an idea in motion.'}</h2></div>
      <${Tooltip} content="Refresh jobs and capabilities"><${Button} variant="text" iconOnly aria-label="Refresh studio" disabled=${busy} onClick=${() => setRevision(n => n + 1)}><${Icon} name="refresh" size=${16}/><//><//></header>
    ${status && html`<div className="studio-status"><${MessageBar} tone=${status.tone}>${status.text}<//></div>`}
    ${loading ? html`<div className="studio-initial">Loading studio controls…</div>` : html`<form className="studio-form" onSubmit=${submit}>
      ${kind === 'video' && html`<div className="studio-video-provider"><${Field} label="Video provider"><select aria-label="Video provider" value=${draft.provider || 'google'} disabled=${locked} onChange=${e => {setError(''); setDraft(d => selectVideoProvider(d, e.target.value, cap));}}>
        ${!provider && html`<option value=${draft.provider}>${draft.provider} · Unavailable</option>`}
        ${videoProviders(cap).map(row => html`<option key=${row.id} value=${row.id}>${row.label || row.id}${row.available ? '' : ' · Not configured'}</option>`)}</select><//></div>`}
      <div className="studio-mode-row"><${Field} label="Operation"><select aria-label="Operation" value=${draft.operation} disabled=${locked} onChange=${e => changedOperation(e.target.value)}>
        ${!operations.includes(draft.operation) && html`<option value=${draft.operation}>${labels[draft.operation] || draft.operation} · Unsupported</option>`}
        ${operations.map(op => html`<option key=${op} value=${op}>${labels[op] || op}</option>`)}</select><//>
        ${openrouter ? html`<${Field} label="OpenRouter model path" hint="Search by name or enter an exact provider/model path."><input aria-label="OpenRouter model path" type="search" list=${`studio-video-models-${requestId}`} value=${draft.model} disabled=${locked} autoComplete="off" spellCheck="false" maxLength="180"
          placeholder="minimax/hailuo-3" aria-describedby=${`studio-video-model-status-${requestId}`} onChange=${e => {setError(''); setDraft({model: e.target.value, showResult: false});}}/>
          <datalist id=${`studio-video-models-${requestId}`}>${currentCatalog.items.map(row => html`<option key=${row.id} value=${row.id}>${row.name || row.id}</option>`)}</datalist><//>`
        : html`<${Field} label="Model">${localBackground ? html`<div className="studio-local-model">Local matte · No model call</div>` : html`<select value=${draft.model} disabled=${locked || !(kind === 'video' ? provider : cap)?.models?.length} onChange=${e => setDraft({model: e.target.value})}>
          ${kind === 'video' && !(provider?.models || []).some(row => row.id === draft.model) && html`<option value=${draft.model}>${draft.model || 'Choose a model'} · Unavailable</option>`}
          ${((kind === 'video' ? provider : cap)?.models || []).map(model => html`<option key=${model.id} value=${model.id}>${model.label || model.name || model.id}</option>`)}</select>`}<//>`}</div>
      ${openrouter && html`<div className="studio-video-model-status" id=${`studio-video-model-status-${requestId}`} role="status" aria-live="polite">
        ${!provider?.available ? 'Configure OpenRouter on the host to search models and generate video. Your model path is saved.'
          : currentCatalog.status === 'error' ? html`<span>Could not load the OpenRouter video catalog: ${currentCatalog.error} Your model path is saved.</span><${Button} type="button" variant="text" size="sm" disabled=${locked} onClick=${() => setCatalogRetry(n => n + 1)}>Retry model search<//>`
          : currentCatalog.status !== 'ready' ? 'Searching the live OpenRouter video catalog…'
          : selectedModel ? html`<strong>${selectedModel.name || selectedModel.id}</strong>${selectedModel.description && html`<p>${selectedModel.description}</p>`}`
          : html`<span>${currentCatalog.items.length ? `${currentCatalog.items.length} matching models. Choose an exact path from the suggestions.` : 'No matching video model found. Check the exact provider/model path.'}${currentCatalog.next_offset != null ? ' Refine your search to see more matches.' : ''}</span>`}
        ${selectedModel && html`<small>Options come from the live catalog. Provider default leaves an option unset.${draft.operation === 'reference' ? ' Reference support requires a provider check; the catalog does not declare it.' : ''}</small>`}
      </div>`}
      ${kind === 'video' && videoError && (!openrouter || selectedModel) && html`<p className="studio-video-validation" role="status">${videoError}</p>`}
      ${kind === 'image' && draft.operation === 'remove_background' && html`<div className="studio-background-method"><${Field} label="Background removal method"><select value=${draft.background_method} disabled=${locked} onChange=${e => setDraft({background_method: e.target.value, showResult: false})}>${(cap?.background_methods || []).map(method => html`<option key=${method.id} value=${method.id} disabled=${!method.available}>${method.label}${method.available ? '' : ' · Not configured'}</option>`)}</select><//></div>`}
      <p className="studio-operation-note">${localBackground ? 'Keep the source pixels and remove their background locally. Draw a region around the subject if needed. Inspect the cutout and refine its background mask before using it.' : kind === 'video' && draft.operation === 'edit' ? 'Add one video and describe the changes to its scene or motion.' : instructions[draft.operation]}</p>
      <div className=${`studio-stage ${active ? 'working' : ''}`}>
        ${active ? html`<${WaitingField} kind=${localJob ? 'cutout' : kind}/>` : resultVisible ? html`<div className="studio-result-preview">${kind === 'image' ? html`<img src=${output.url} alt=${localJob ? 'Source-preserving cutout' : 'Generated image'}/>` : html`<video src=${output.url} controls playsInline preload="metadata"/>`}<span>${output.width && output.height ? `${output.width} × ${output.height}` : ''}${output.seconds ? ` · ${output.seconds.toFixed(1)} s` : ''}</span></div>`
          : source && source.kind !== 'video' && source.width && source.height ? html`<${SourceCanvas} draft=${draft} setDraft=${setDraft} size=${size} disabled=${locked} onError=${setError}/>`
          : source?.kind === 'video' ? html`<div className="studio-result-preview"><video src=${source.url} controls playsInline preload="metadata"/><span>Source video</span></div>`
          : html`<div className="studio-empty"><div className="studio-empty-frame" style=${{aspectRatio: kind === 'image' ? `${size[0]}/${size[1]}` : (draft.aspect || '16:9').replace(':', '/')}}><${Icon} name=${kind === 'image' ? 'sparkles' : 'file'} size=${18}/></div>
            <strong>${kind === 'image' ? 'Your next image starts here' : 'Your next clip starts here'}</strong><p>${maxInputs ? 'Drop a file from your computer or workspace, or start with a prompt below.' : 'Write a prompt below, or choose an operation that uses a source.'}</p>
            ${maxInputs > 0 && html`<${Button} variant="secondary" size="sm" type="button" disabled=${locked} onClick=${() => uploadInput.current.click()}><${Icon} name="upload" size=${14}/>Add ${acceptsVideo ? 'video' : 'image'}<//>`}</div>`}
      </div>
      ${resultVisible && html`<div className="studio-result-actions">${(kind === 'image' || operations.includes('edit')) && html`<${Button} type="button" size="sm" variant="secondary" onClick=${() => useOutput(output)}>Edit this ${kind}<//>`}
        ${job.result.background_mask && html`<${Button} type="button" size="sm" variant="secondary" disabled=${busy} onClick=${useBackgroundMask}>Use background mask<//>`}
        <${Button} type="button" size="sm" variant="text" onClick=${() => onOpen?.({id: `file:${output.path}`, kind: 'file', title: output.path.split('/').at(-1), path: output.path})}>Open file<//>
        <a href=${output.url} download>Download</a><${Button} type="button" size="sm" variant="text" onClick=${() => setDraft({showResult: false})}>Back to source<//></div>`}
      ${resultVisible && html`<details className="studio-output-details"><summary>Generation details</summary><dl>
        ${kind === 'video' && html`<dt>Provider</dt><dd>${job.input?.provider || 'google'}</dd>`}<dt>Model</dt><dd>${localJob ? 'Local matte. No model call.' : job.result.model || job.input?.model}</dd><dt>Operation</dt><dd>${labels[job.operation] || job.operation}</dd>
        ${kind === 'image' && !localJob && html`<dt>Effort</dt><dd>${job.input?.quality}</dd>`}<dt>File</dt><dd>${output.path}</dd>
        <dt>Provenance</dt><dd>${typeof output.provenance === 'string' ? output.provenance : localJob ? 'Source pixels retained. Inspect the segmentation and receipt.' : 'Generated. See the receipt for source lineage.'}</dd>
        ${(job.result.warnings || []).length > 0 && html`<dt>Checks</dt><dd>${job.result.warnings.join(' ')}</dd>`}</dl>
        ${job.result.receipt_path && html`<${Button} type="button" variant="text" size="sm" onClick=${() => onOpen?.({id: `file:${job.result.receipt_path}`, kind: 'file', title: 'Generation receipt', path: job.result.receipt_path})}>Open receipt<//>`}</details>`}
      ${kind === 'video' && job?.provider_job && html`<details className="studio-output-details studio-provider-job" open=${['failed', 'cancelled', 'interrupted'].includes(job.status)}><summary>Provider job details</summary><dl>
        <dt>Provider</dt><dd>${job.provider_job.provider || job.input?.provider}</dd>
        <dt>Model</dt><dd>${job.provider_job.model || job.input?.model}</dd>
        <dt>Job ID</dt><dd>${job.provider_job.id || 'Not recorded'}</dd>
        <dt>Last status</dt><dd>${job.provider_job.status || 'Unknown'}</dd></dl>
        ${['failed', 'cancelled', 'interrupted'].includes(job.status) && html`<p>Local ${job.status === 'cancelled' ? 'cancellation' : 'failure'} does not confirm that the provider stopped. Check this job before submitting another paid request.</p>`}
      </details>`}
      ${!resultVisible && source && kind === 'image' && html`<div className="studio-canvas-tools">
        ${draft.operation === 'edit' && html`<${Button} type="button" size="sm" variant="secondary" pressed=${!!draft.markup} disabled=${locked} onClick=${() => setDraft(d => ({...d, markup: !d.markup}))}><${Icon} name="pen" size=${14}/>Markup<//>`}
        ${drawing && html`<div className="studio-segment" aria-label="Drawing tool"><button type="button" aria-pressed=${draft.tool === 'brush'} disabled=${locked} onClick=${() => setDraft({tool: 'brush'})}>Brush</button><button type="button" aria-pressed=${draft.tool === 'erase'} disabled=${locked} onClick=${() => setDraft({tool: 'erase'})}>Erase</button></div>
          <label className="studio-brush">Size<input type="range" min="4" max=${Math.max(100, Math.round(Math.min(source.width, source.height) / 3))} value=${draft.brush} disabled=${locked} onChange=${e => setDraft({brush: Number(e.target.value)})}/><span>${draft.brush} px</span></label>
          ${draft.operation === 'inpaint' && html`<${Button} type="button" variant="text" size="sm" disabled=${locked} onClick=${() => setDraft(d => ({...d, strokes: [...d.strokes, {width: Math.ceil(Math.hypot(source.width, source.height)) * 2, points: [[source.width / 2, source.height / 2]], erase: false}]}))}>Select all<//>`}
          <${IconAction} name="arrow-left" label="Undo last stroke" type="button" disabled=${locked || !draft.strokes.length} onClick=${() => setDraft(d => ({...d, strokes: d.strokes.slice(0, -1)}))}/>
          <${IconAction} name="trash" label="Clear drawing" type="button" disabled=${locked || !draft.strokes.length && !draft.savedMask} onClick=${() => setDraft({strokes: [], savedMask: null})}/>`}
        ${draft.operation === 'outpaint' && html`<${IconAction} name="minus" label="Scale image down" type="button" disabled=${locked} onClick=${() => setDraft(d => ({...d, place: scalePlacement(d.place || fitPlacement(source, size), .9, size)}))}/>
          <${IconAction} name="plus" label="Scale image up" type="button" disabled=${locked} onClick=${() => setDraft(d => ({...d, place: scalePlacement(d.place || fitPlacement(source, size), 1.1, size)}))}/>
          <${Button} type="button" variant="text" size="sm" disabled=${locked} onClick=${() => setDraft({place: fitPlacement(source, size)})}>Fit and center<//>
          ${[0, 1].map(index => html`<label className="studio-position-input" key=${index}>${index ? 'Y' : 'X'}<input type="number" min="0" max=${size[index] - (draft.place || fitPlacement(source, size))[index + 2]} value=${(draft.place || fitPlacement(source, size))[index]} disabled=${locked} onChange=${e => setDraft(d => {const p = d.place || fitPlacement(source, size), delta = Number(e.target.value) - p[index]; return {...d, place: movePlacement(p, index ? 0 : delta, index ? delta : 0, size)};})}/></label>`)}
          <span className="studio-tool-hint">${(draft.place || fitPlacement(source, size)).join(', ')} px · x, y, width, height</span>`}
        ${draft.operation === 'remove_background' && html`<span className="studio-tool-hint">${draft.box ? `Selected region: ${draft.box.join(', ')} px` : 'Whole image selected'}</span><${Button} type="button" variant="text" size="sm" disabled=${locked || !draft.box} onClick=${() => setDraft({box: null})}>Use whole image<//>
          <details className="studio-region-fields"><summary>Set region in pixels</summary><div>${['X', 'Y', 'Width', 'Height'].map((label, index) => html`<label className="studio-position-input" key=${label}>${label}<input type="number" min=${index < 2 ? 0 : 1} value=${(draft.box || [0, 0, source.width, source.height])[index]} disabled=${locked} onChange=${e => setDraft(d => {const b = [...(d.box || [0, 0, source.width, source.height])]; b[index] = Math.max(index < 2 ? 0 : 1, Number(e.target.value)); b[0] = Math.min(b[0], source.width - 1); b[1] = Math.min(b[1], source.height - 1); b[2] = Math.min(b[2], source.width - b[0]); b[3] = Math.min(b[3], source.height - b[1]); return {...d, box: b.map(Math.round)};})}/></label>`)}</div></details>`}
      </div>`}
      <div className="studio-inputs">${draft.sources.map((s, index) => html`<div className="studio-source" key=${`${s.path}:${index}`}>
        ${s.kind !== 'video' ? html`<img src=${s.url} alt=""/>` : html`<${Icon} name="file" size=${16}/>`}<span title=${s.path}>${draft.operation === 'frames' ? index ? 'Last frame' : 'First frame' : index ? `Reference ${index + 1}` : 'Source'}<small>${s.name || s.path?.split('/').at(-1)}</small></span>
        <${IconAction} name="x" label=${`Remove ${s.name || 'source'}`} type="button" disabled=${locked} onClick=${() => setDraft(d => ({...d, sources: d.sources.filter((_, i) => i !== index), strokes: [], savedMask: null, box: null, place: null, showResult: false}))}/></div>`)}
        ${maxInputs > draft.sources.length && html`<${Button} type="button" variant="text" size="sm" disabled=${locked} onClick=${() => uploadInput.current.click()}><${Icon} name="plus" size=${14}/>Add ${draft.sources.length ? 'reference' : acceptsVideo ? 'video' : 'image'}<//>
          <${Button} type="button" variant="text" size="sm" disabled=${locked} onClick=${() => setShowPath(p => !p)}>From workspace<//>`}
        <input className="studio-file-input" ref=${uploadInput} type="file" accept=${acceptsVideo ? 'video/*' : 'image/png,image/jpeg,image/webp'} multiple=${maxInputs > 1} aria-label="Upload studio source" disabled=${locked} onChange=${e => {addSources(Array.from(e.target.files)); e.target.value = '';}}/>
      </div>
      ${showPath && html`<div className="studio-path-input"><label>Workspace file path<input value=${workspacePath} placeholder="work/image.png" onChange=${e => setWorkspacePath(e.target.value)} onKeyDown=${e => {if (e.key === 'Enter') {e.preventDefault(); addWorkspace(workspacePath);}}}/></label><${Button} type="button" size="sm" variant="secondary" disabled=${locked || !workspacePath.trim()} onClick=${() => addWorkspace(workspacePath.trim())}>Add file<//></div>`}
      <div className="studio-prompt-panel">${localBackground ? html`<p className="studio-local-note">Local segmentation is approximate. It creates a cutout and a source-sized background mask you can refine in Inpaint. No provider request is made.</p>` : draft.operation === 'remove_background' ? html`<${Field} label="Subject to keep"><textarea rows="2" value=${draft.subject} disabled=${locked} maxLength="2000" placeholder="The glass bottle in the foreground" onChange=${e => setDraft({subject: e.target.value})}/><//>`
        : html`<${Field} label=${kind === 'image' ? 'Describe your image' : 'Describe your video'}><textarea rows="3" value=${draft.prompt} disabled=${locked} maxLength="20000" placeholder=${draft.operation === 'inpaint' ? 'What should replace the marked area?' : draft.operation === 'outpaint' ? 'Describe the space around your image…' : kind === 'image' ? 'A subject, a setting, a mood. Make it yours…' : 'Describe the scene, movement and camera…'} onChange=${e => setDraft({prompt: e.target.value})}/><//>`}
        <div className=${`studio-settings ${kind === 'video' ? 'studio-video-settings' : ''}`}>${kind === 'image' ? html`
          ${!['inpaint', 'remove_background'].includes(draft.operation) && html`<${Field} label="Width"><input type="number" min="16" max=${cap?.size_constraints?.max_edge || 3840} step="16" value=${draft.size[0]} disabled=${locked} onChange=${e => updateSize(0, e.target.value)}/><//><span className="studio-size-times">×</span><${Field} label="Height"><input type="number" min="16" max=${cap?.size_constraints?.max_edge || 3840} step="16" value=${draft.size[1]} disabled=${locked} onChange=${e => updateSize(1, e.target.value)}/><//>`}
          ${!localBackground && html`<${Field} label="Effort"><select value=${draft.quality} disabled=${locked} onChange=${e => setDraft({quality: e.target.value})}>${(cap?.quality || []).map(q => html`<option key=${q} value=${q}>${q === 'xhigh' ? 'Extra high' : q[0].toUpperCase() + q.slice(1)}</option>`)}</select><//>`}
          ${['create', 'edit'].includes(draft.operation) && html`<label className="studio-check"><input type="checkbox" checked=${draft.transparent} disabled=${locked} onChange=${e => setDraft({transparent: e.target.checked})}/>Transparent background</label><label className="studio-check"><input type="checkbox" checked=${draft.allow_text} disabled=${locked} onChange=${e => setDraft({allow_text: e.target.checked})}/>Allow text</label>`}
          ` : html`<${VideoOption} label="Duration" value=${draft.seconds} values=${options.seconds} suffix=" seconds" disabled=${locked || openrouter && !selectedModel} onChange=${e => setDraft({seconds: e.target.value === '' ? '' : Number(e.target.value)})}/>
            <${VideoOption} label="Resolution" value=${draft.resolution} values=${options.resolutions} disabled=${locked || openrouter && !selectedModel} onChange=${e => setDraft({resolution: e.target.value})}/>
            <${VideoOption} label="Aspect" value=${draft.aspect} values=${options.aspects} disabled=${locked || openrouter && !selectedModel} onChange=${e => setDraft({aspect: e.target.value})}/>
            ${options.audio && html`<${Field} label="Generate audio"><select value=${draft.generate_audio === true ? 'true' : draft.generate_audio === false ? 'false' : ''} disabled=${locked} onChange=${e => setDraft({generate_audio: e.target.value === '' ? '' : e.target.value === 'true'})}>
              <option value="">Provider default</option><option value="true">On</option><option value="false">Off</option></select><//>`}
            ${options.seed && html`<${Field} label="Seed"><input type="number" min="0" max="2147483647" step="1" value=${draft.seed ?? ''} placeholder="Random" disabled=${locked} onChange=${e => setDraft({seed: e.target.value})}/><//>`}
            <${Button} type="button" variant="text" size="sm" disabled=${locked} onClick=${() => setDraft({seconds: '', resolution: '', aspect: '', generate_audio: '', seed: ''})}>Reset video options<//>`}</div>
        <div className="studio-submit-row"><span>${kind === 'image' && !['inpaint', 'remove_background'].includes(draft.operation) ? `Canvas ${size[0]} × ${size[1]} px. ` : ''}Saved in work/studio. ${(active ? localJob : localBackground) ? 'No provider charges.' : 'Provider charges apply.'}</span>
          ${active ? html`<${Button} type="button" variant="secondary" size="sm" disabled=${busy} onClick=${cancel}><${Icon} name="square" size=${14}/>Stop job<//>` : html`<${Button} type="submit" variant="secondary" disabled=${busy || !canRun || !!videoError || !localBackground && !draft.model} loading=${busy}><${Icon} name="sparkles" size=${14}/>${draft.operation === 'remove_background' ? 'Remove background' : `Generate ${kind}`}<//>`}</div>
      </div>
      ${jobs.length > 0 && html`<details className="studio-history"><summary>Recent generations <span>${jobs.length}</span></summary><div>${jobs.map(previous => html`<div className="studio-history-row" key=${previous.id}><button type="button" disabled=${active || busy} onClick=${() => {setJob(previous); setDraft({jobId: previous.id, showResult: previous.status === 'completed'});}}><strong>${labels[previous.operation] || previous.operation}</strong><span>${previous.status}</span><small>${previous.created ? new Date(typeof previous.created === 'number' ? previous.created * 1000 : previous.created).toLocaleString() : ''}</small></button>
        <${Button} variant="text" type="button" size="sm" disabled=${locked || !previous.input} onClick=${() => reuse(previous)}>Reuse settings<//>
        ${previous.result?.receipt_path && html`<${IconAction} label="Open generation receipt" name="file-text" type="button" onClick=${() => onOpen?.({id: `file:${previous.result.receipt_path}`, kind: 'file', title: 'Generation receipt', path: previous.result.receipt_path})}/>`}</div>`)}</div></details>`}
    </form>`}
    ${dragging && html`<div className="studio-drop-veil"><${Icon} name="upload" size=${18}/><strong>${locked ? 'Wait for the current job before adding a source' : 'Drop into this studio'}</strong><span>${acceptsVideo ? 'Video source' : 'Image source or reference'}</span></div>`}
  </section>`;
}
