// Pixel geometry is independent of the responsive preview's CSS size.
export const WORKSPACE_FILE_MIME = 'application/x-vwb-workspace-file';
export const isActiveJob = job => ['queued', 'running'].includes(job?.status);
export const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));
export function pointInCanvas(clientX, clientY, rect, width, height) {
  return [clamp((clientX - rect.left) * width / Math.max(1, rect.width), 0, width),
    clamp((clientY - rect.top) * height / Math.max(1, rect.height), 0, height)];
}
export function fitPlacement(source, size) {
  const scale = Math.min(size[0] / source.width, size[1] / source.height) * .8;
  const width = Math.max(1, Math.round(source.width * scale)), height = Math.max(1, Math.round(source.height * scale));
  return [Math.round((size[0] - width) / 2), Math.round((size[1] - height) / 2), width, height];
}
export function scalePlacement(place, factor, size) {
  const scale = Math.min(factor, size[0] / place[2], size[1] / place[3]);
  const w = clamp(Math.round(place[2] * scale), 1, size[0]), h = clamp(Math.round(place[3] * scale), 1, size[1]);
  return [clamp(Math.round(place[0] + (place[2] - w) / 2), 0, size[0] - w),
    clamp(Math.round(place[1] + (place[3] - h) / 2), 0, size[1] - h), w, h];
}
export function movePlacement(place, dx, dy, size) {
  return [clamp(Math.round(place[0] + dx), 0, Math.max(0, size[0] - place[2])),
    clamp(Math.round(place[1] + dy), 0, Math.max(0, size[1] - place[3])), place[2], place[3]];
}
export function boxFromPoints(a, b, size) {
  const x = clamp(Math.floor(Math.min(a[0], b[0])), 0, size[0] - 1), y = clamp(Math.floor(Math.min(a[1], b[1])), 0, size[1] - 1);
  return [x, y, Math.max(1, Math.min(size[0] - x, Math.ceil(Math.abs(a[0] - b[0])))),
    Math.max(1, Math.min(size[1] - y, Math.ceil(Math.abs(a[1] - b[1]))))];
}
const roundEven = n => n % 1 === .5 ? 2 * Math.round(n / 2) : Math.round(n);
// Mirrors the image adapter's valid_size; the job response remains authoritative.
export function imageSize(size, limits = {}) {
  let [w, h] = size.map(Number);
  if (![w, h].every(n => Number.isFinite(n) && n >= 16 && n <= (limits.max_edge || 3840))) return null;
  if (Math.max(w, h) / Math.min(w, h) > (limits.max_aspect || 3)) return null;
  const step = limits.multiple || 16;
  for (let n = 0; n < 40; n++) {
    const W = Math.max(step, roundEven(w / step) * step), H = Math.max(step, roundEven(h / step) * step);
    if (W * H < (limits.min_pixels || 655360)) {w *= 1.06; h *= 1.06;}
    else if (W * H > (limits.max_pixels || 8294400) || Math.max(W, H) > (limits.max_edge || 3840)) {w /= 1.06; h /= 1.06;}
    else return [W, H];
  }
  return null;
}
export function workspaceDrop(raw, requestId) {
  let data;
  try {data = JSON.parse(raw);} catch {throw new Error('This workspace file could not be read. Drag it again.');}
  if (data.requestId !== requestId) throw new Error('Choose a file from this conversation’s workspace.');
  if (typeof data.path !== 'string' || !data.path || data.path.startsWith('/') || data.path.split('/').some(p => p === '..')) throw new Error('This workspace path is not supported.');
  return data;
}
export function inputLimit(kind, operation) {
  if (kind === 'image') return ['inpaint', 'outpaint', 'remove_background'].includes(operation) ? 1 : 16;
  return operation === 'text' ? 0 : operation === 'frames' ? 2 : operation === 'reference' ? 3 : 1;
}
// Provider and catalog metadata stay separate: unknown OpenRouter options must
// not inherit Google's defaults. The host revalidates the live catalog on submit.
export function videoProviders(cap = {}) {
  return Array.isArray(cap?.providers) ? cap.providers : [{...cap, id: 'google', label: 'Google / Omni'}];
}
export function videoProvider(cap, id = 'google') {
  return videoProviders(cap).find(provider => provider.id === id);
}
const videoFields = ['model', 'seconds', 'resolution', 'aspect', 'generate_audio', 'seed'];
export function selectVideoProvider(draft, id, cap) {
  const previous = draft.provider || 'google', provider = videoProvider(cap, id);
  const saved = {...draft.videoPreferences, [previous]: Object.fromEntries(videoFields.map(field => [field, draft[field]]))};
  const initial = id === 'google' ? {seconds: 6, resolution: provider?.default_resolution || '1080p', aspect: provider?.default_aspect || '9:16'}
    : {seconds: '', resolution: '', aspect: ''};
  const model = provider?.default_model || (provider?.models?.find(row => row.default) || provider?.models?.[0])?.id || '';
  return {...draft, ...initial, model, generate_audio: '', seed: '', ...saved[id], provider: id, videoPreferences: saved, showResult: false};
}
const strings = values => Array.isArray(values) ? [...new Set(values.filter(value => typeof value === 'string' && value))] : [];
export function videoOptions(provider, model) {
  if (provider?.id === 'openrouter') return {
    seconds: Array.isArray(model?.supported_durations) ? [...new Set(model.supported_durations.filter(value => Number.isFinite(value) && value > 0))] : [],
    resolutions: strings(model?.supported_resolutions), aspects: strings(model?.supported_aspect_ratios),
    audio: model?.generate_audio === true, seed: model?.seed === true,
    frames: Array.isArray(model?.supported_frame_images) ? model.supported_frame_images : [],
  };
  const min = provider?.seconds?.min, max = provider?.seconds?.max;
  return {seconds: Number.isInteger(min) && Number.isInteger(max) && min > 0 && max >= min && max - min < 1000
    ? Array.from({length: max - min + 1}, (_, index) => min + index) : [],
  resolutions: strings(provider?.resolutions), aspects: strings(provider?.aspects), audio: false, seed: false, frames: null};
}
const hasValue = value => value !== '' && value !== null && value !== undefined;
export function videoSettingsError(draft, provider, model) {
  if (!provider || provider.id !== (draft.provider || 'google')) return 'This video provider is not offered by this server. Your selection is preserved.';
  if (!(provider.operations || []).includes(draft.operation)) return 'Choose an operation supported by this provider. Your sources are still attached.';
  if (provider.id === 'openrouter' && (!model || model.id !== (draft.model || '').trim())) return 'Choose an exact OpenRouter model path from the live catalog.';
  if (provider.id === 'google' && !(provider.models || []).some(row => row.id === draft.model)) return 'Choose an available Google video model. Your saved selection has not been replaced.';
  const options = videoOptions(provider, model);
  for (const [field, values, label] of [['seconds', options.seconds, 'Duration'], ['resolution', options.resolutions, 'Resolution'], ['aspect', options.aspects, 'Aspect']]) {
    if (hasValue(draft[field]) && !values.some(value => String(value) === String(draft[field]))) return `${label} ${draft[field]} is not advertised for this model. Choose a supported value or Provider default.`;
  }
  if (hasValue(draft.generate_audio) && (!options.audio || typeof draft.generate_audio !== 'boolean')) return 'The saved audio option is not advertised for this model. Reset video options to use provider defaults.';
  if (hasValue(draft.seed) && (!options.seed || !Number.isInteger(Number(draft.seed)) || Number(draft.seed) < 0 || Number(draft.seed) > 2147483647)) return 'The saved seed is not supported or is invalid. Reset video options or enter a whole number from 0 to 2147483647.';
  const sources = draft.sources || [];
  if (sources.length > inputLimit('video', draft.operation)) return 'Remove extra sources or choose a different operation. No sources were removed automatically.';
  if (sources.some(source => (source.kind === 'video') !== ['edit', 'extend'].includes(draft.operation))) return 'The attached source type does not match this operation. Remove it or change operation.';
  if (draft.operation === 'frames' && options.frames && sources.length) {
    if (!options.frames.includes('first_frame')) return 'This model does not advertise first-frame input. Choose another model or operation.';
    if (sources.length > 1 && !options.frames.includes('last_frame')) return 'This model does not advertise last-frame input. Remove the last frame or choose another model.';
  }
  return '';
}
export function draftDefaults(kind) {
  return {operation: kind === 'image' ? 'create' : 'text', prompt: '', subject: '', background_method: 'matte', model: '', quality: 'high',
    ...(kind === 'video' ? {provider: 'google', generate_audio: '', seed: ''} : {}),
    size: [1024, 1024], transparent: false, allow_text: true, seconds: 6, resolution: '1080p', aspect: '9:16',
    sources: [], strokes: [], box: null, place: null, brush: 40, tool: 'brush', jobId: null};
}
export function inputRef(source) {
  return source.upload_id ? {upload_id: source.upload_id} : source.ref ? {ref: source.ref} : {path: source.path};
}
export function requestBody(draft, kind, idempotencyKey, mask = null, limits = {}) {
  const op = draft.operation, sources = draft.sources || [];
  if (op !== 'remove_background' && !draft.prompt.trim()) throw new Error('Describe the result you want.');
  if (op === 'remove_background' && draft.background_method === 'sunburst' && !draft.subject.trim()) throw new Error('Describe the subject you want to keep.');
  if ((kind === 'image' && op !== 'create' || kind === 'video' && op !== 'text') && !sources.length) throw new Error('Add a source file first.');
  if (sources.length > inputLimit(kind, op)) throw new Error('Remove extra sources before generating.');
  if (op === 'inpaint' && !mask) throw new Error('Paint the pixels you want to replace.');
  const size = ['inpaint', 'remove_background'].includes(op) ? [1024, 1024] : imageSize(draft.size, limits);
  if (kind === 'image' && !size) throw new Error('Choose dimensions between 16 and 3840 pixels, with an aspect ratio no wider than 3:1.');
  const data = {idempotency_key: idempotencyKey, kind, operation: op, prompt: draft.prompt.trim(), model: draft.model || null,
    inputs: sources.map(inputRef)};
  if (kind === 'image') Object.assign(data, {quality: draft.quality, size, transparent: draft.transparent, allow_text: draft.allow_text});
  else {
    data.provider = draft.provider || 'google';
    data.model = (draft.model || '').trim() || null;
    // Explicit null is required: omitted fields retain legacy Google defaults
    // in the job schema, including when the selected provider is OpenRouter.
    Object.assign(data, {seconds: null, resolution: null, aspect: null});
    if (data.provider === 'openrouter' && !data.model) throw new Error('Enter an OpenRouter model path.');
    if (hasValue(draft.seconds)) {
      if (!Number.isInteger(Number(draft.seconds)) || Number(draft.seconds) <= 0 || Number(draft.seconds) > 600) throw new Error('Choose a whole-number duration from 1 to 600 seconds.');
      data.seconds = Number(draft.seconds);
    }
    for (const field of ['resolution', 'aspect']) if (hasValue(draft[field])) data[field] = draft[field];
    if (hasValue(draft.generate_audio)) {
      if (typeof draft.generate_audio !== 'boolean') throw new Error('Choose an audio option.');
      data.generate_audio = draft.generate_audio;
    }
    if (hasValue(draft.seed)) {
      if (!Number.isInteger(Number(draft.seed)) || Number(draft.seed) < 0 || Number(draft.seed) > 2147483647) throw new Error('Enter a whole-number seed from 0 to 2147483647.');
      data.seed = Number(draft.seed);
    }
  }
  if (op === 'inpaint') data.mask = mask;
  if (op === 'outpaint') data.place = draft.place || fitPlacement(sources[0], size);
  if (op === 'remove_background') {data.background_method = draft.background_method || 'matte'; data.subject = draft.subject.trim(); if (draft.box) data.box = draft.box;}
  return data;
}
export function paintStrokes(ctx, strokes, color = '#ffffff') {
  for (const stroke of strokes) {
    if (!stroke.points.length) continue;
    ctx.globalCompositeOperation = stroke.erase ? 'destination-out' : 'source-over';
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = stroke.width; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    if (stroke.points.length === 1) {ctx.beginPath(); ctx.arc(...stroke.points[0], stroke.width / 2, 0, Math.PI * 2); ctx.fill();}
    else {ctx.beginPath(); ctx.moveTo(...stroke.points[0]); for (const point of stroke.points.slice(1)) ctx.lineTo(...point); ctx.stroke();}
  }
  ctx.globalCompositeOperation = 'source-over';
}
export function safeDraft(draft) {
  // URL credentials and bitmap data never enter session storage.
  const {url, ...savedMask} = draft.savedMask || {};
  return {...draft, sources: draft.sources.map(({url, ...source}) => source), savedMask: draft.savedMask ? savedMask : null,
    strokes: draft.strokes.slice(-1000)};
}
