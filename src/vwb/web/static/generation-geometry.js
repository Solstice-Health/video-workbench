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
export function draftDefaults(kind) {
  return {operation: kind === 'image' ? 'create' : 'text', prompt: '', subject: '', background_method: 'matte', model: '', quality: 'high',
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
  else Object.assign(data, {seconds: Number(draft.seconds), resolution: draft.resolution, aspect: draft.aspect});
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
