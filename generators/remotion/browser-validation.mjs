// This boundary accepts compiled data, never HTML, JavaScript, CSS declarations,
// file paths to source, or arbitrary assets. Unknown fields fail closed.
import { isAbsolute } from 'node:path';
import { readFileSync } from 'node:fs';
const manifest = JSON.parse(readFileSync(new URL('./public/fonts/manifest.json', import.meta.url), 'utf8'));
const families = new Set(manifest.map(m => m.family));
export const LIMITS = Object.freeze({ jobs: 64, layers: 512, dimension: 4096, pixels: 16777216,
  frames: 36000, batchFrames: 50000, pixelFrames: 128000000000, jsonBytes: 16 * 1024 * 1024,
  frameMs: 30000, jobMs: 30 * 60 * 1000, batchMs: 4 * 60 * 60 * 1000 });
export const RENDERER_PINS = Object.freeze({
  browser: 'browser-scene@1:playwright-core@1.64.1:esbuild@0.28.2',
  gsap: 'gsap-scene@1:gsap@3.15.0:playwright-core@1.64.1:esbuild@0.28.2',
  canvas2d: 'canvas2d-scene@1:playwright-core@1.64.1:esbuild@0.28.2',
});
export const CAPABILITIES = Object.freeze({
  browser: ['shape', 'text', 'captions'], gsap: ['shape', 'text', 'captions'], canvas2d: ['shape'],
});
function check(ok, message) { if (!ok) throw new Error(message); }
function object(v, keys, label) {
  check(v !== null && typeof v === 'object' && !Array.isArray(v), `${label}: expected object`);
  for (const k of Object.keys(v)) check(keys.includes(k), `${label}: unsupported field ${k}`);
}
const num = (v, name, lo = -1000000, hi = 1000000) => check(typeof v === 'number' && Number.isFinite(v) && v >= lo && v <= hi, `${name}: expected number in [${lo}, ${hi}]`);
const integer = (v, name, lo = 0, hi = 10000000) => { num(v, name, lo, hi); check(Number.isInteger(v), `${name}: expected integer`); };
const string = (v, name, max = 65536) => check(typeof v === 'string' && v.length <= max && !v.includes('\0'), `${name}: expected bounded string`);
const oneOf = (v, values, name) => check(values.includes(v), `${name}: expected ${values.join('|')}`);
const optional = (v, fn) => { if (v !== undefined && v !== null) fn(v); };
const boolean = (v, n) => check(typeof v === 'boolean', `${n}: expected boolean`);
function vector(v, n, label, size = false) {
  check(Array.isArray(v) && v.length === n, `${label}: expected ${n} numbers`);
  v.forEach((x, i) => num(x, label, size || n === 4 && i > 1 ? 0 : -1000000));
}
function color(v) {
  string(v, 'color', 100);
  check(v === 'none' || v.toLowerCase() !== 'none', 'color: none must be lowercase');
  // A finite CSS color grammar. No url(), var(), escapes, or CSS declarations.
  check(/^#(?:[\da-f]{3}|[\da-f]{4}|[\da-f]{6}|[\da-f]{8})$/i.test(v) || /^(?:transparent|none|black|white|red|green|blue|yellow|cyan|magenta|gray|grey|orange|purple)$/i.test(v),
    'color: use a hex color, transparent, none, or a basic named color');
}
function animated(v, validate, label) {
  if (Array.isArray(v) && typeof v[0] === 'object' && v[0] !== null) {
    check(v.length <= 4096, `${label}: too many keys`);
    let previous = -1;
    for (const key of v) {
      object(key, ['f', 'v', 'ease'], label);
      integer(key.f, `${label}.f`); check(key.f > previous, `${label}: keys must be strictly increasing`); previous = key.f;
      optional(key.ease, x => oneOf(x, ['hold', 'linear', 'in', 'out', 'inout'], 'ease'));
      validate(key.v);
      if (validate === color) check(/^#[\da-f]{6}(?:[\da-f]{2})?$/i.test(key.v), `${label}: animated colors require #RRGGBB or #RRGGBBAA`);
    }
  } else validate(v);
}
function stroke(v) {
  object(v, ['width', 'color', 'dash', 'cap'], 'stroke'); num(v.width, 'stroke.width', 0, 4096); animated(v.color, color, 'stroke.color');
  optional(v.cap, x => oneOf(x, ['butt', 'round', 'square'], 'stroke.cap'));
  optional(v.dash, x => { check(Array.isArray(x) && x.length <= 128, 'stroke.dash: invalid pattern'); x.forEach(n => num(n, 'dash', 0, 100000)); });
}
function pathData(d) {
  string(d, 'path.d', 100000);
  check(/^[MmZzLlHhVvCcSsQqTtAaEe\d\s.,+\-]*$/.test(d), 'path.d: expected SVG geometry, never markup');
}
function style(s) {
  object(s, ['font','weight','size','min_size','color','italic','align','valign','line_height','letter_spacing','uppercase','shadow','stroke','background','padding','radius','accent_bar','highlight','muted'], 'style');
  check(families.has(s.font), 'style.font: must name a pinned font family');
  integer(s.weight, 'style.weight', 100, 900); num(s.size, 'style.size', 1, 4096);
  optional(s.min_size, v => num(v, 'min_size', 1, s.size));
  animated(s.color, color, 'style.color');
  for (const key of ['italic', 'uppercase', 'shadow']) optional(s[key], v => boolean(v, key));
  optional(s.align, v => oneOf(v, ['left','center','right'], 'align'));
  optional(s.valign, v => oneOf(v, ['top','middle','bottom'], 'valign'));
  optional(s.line_height, v => num(v, 'line_height', 0.1, 100));
  optional(s.letter_spacing, v => num(v, 'letter_spacing', -4096, 4096));
  for (const key of ['padding', 'radius']) optional(s[key], v => num(v, key, 0, 4096));
  for (const key of ['background', 'accent_bar', 'highlight', 'muted']) optional(s[key], color);
  optional(s.stroke, v => { object(v, ['width','color'], 'style.stroke'); num(v.width, 'stroke.width', 0, 4096); color(v.color); });
}
function layer(l, renderer) {
  const common = ['id','type','span','opacity','rotate','role','note','box'];
  const fields = { shape: ['shape','fill','stroke','radius','gradient','gradient_angle','d','viewbox','draw','progress','paths','asset'],
    text: ['copy_id','text','style','animate'], captions: ['style','mode','pages','max_chars','max_lines'] };
  check(l && CAPABILITIES[renderer].includes(l.type), `${renderer}: unsupported layer type ${l?.type}; supports ${CAPABILITIES[renderer].join(', ')}`);
  object(l, [...common,...fields[l.type]], 'layer'); string(l.id,'layer.id',128);
  check(/^[A-Za-z0-9_.:-]+$/.test(l.id), 'layer.id: invalid identifier');
  optional(l.role, v => string(v, 'role', 100)); optional(l.note, v => string(v, 'note'));
  optional(l.span, v => { vector(v, 2, 'span', true); v.forEach(x => integer(x,'span')); check(v[1] > v[0], 'span must be increasing'); });
  animated(l.box, v => vector(v,4,'box'), 'box');
  optional(l.opacity, v => animated(v, n => num(n, 'opacity', 0, 1), 'opacity'));
  optional(l.rotate, v => animated(v, n => num(n, 'rotate'), 'rotate'));
  if (l.type === 'shape') {
    oneOf(l.shape, ['rect','circle','line','path','svg'], 'shape');
    optional(l.fill, v => animated(v,color,'fill')); optional(l.stroke,stroke);
    optional(l.radius, v => num(v,'radius',0,4096)); optional(l.gradient_angle, v => num(v,'gradient_angle'));
    optional(l.gradient, v => { check(Array.isArray(v) && v.length <= 64, 'gradient: too many stops');
      v.forEach(stop => { color(stop); check(stop !== 'none', 'gradient: none is not a color stop; use transparent'); }); });
    optional(l.d,pathData); optional(l.progress,v => boolean(v,'progress'));
    optional(l.viewbox,v => { vector(v,2,'viewbox',true); check(v.every(n => n > 0), 'viewbox: dimensions must be positive'); });
    optional(l.draw,v => animated(v,n => num(n,'draw',0,1),'draw'));
    optional(l.asset,v => string(v,'asset',1024)); // inert registry provenance; never read from disk or fetched
    optional(l.paths, v => {
      check(Array.isArray(v) && v.length <= 2048, 'paths: too many paths');
      v.forEach(p => { object(p,['d','fill','stroke','opacity','translate'],'path'); pathData(p.d); optional(p.fill,color);
        optional(p.stroke,stroke); optional(p.opacity,v => num(v,'path.opacity',0,1)); optional(p.translate,v => vector(v,2,'translate')); });
    });
    if (l.shape === 'path' || l.shape === 'svg') check(Boolean(l.d) || Boolean(l.paths?.length), 'path/svg: requires compiled d/paths; unresolved assets are unsupported');
  } else {
    style(l.style);
    if (l.type === 'text') {
      string(l.text,'text'); string(l.copy_id,'copy_id',256);
      optional(l.animate, v => { object(v,['enter','exit','frames'],'animate');
        optional(v.enter,x => oneOf(x,['none','fade','rise','pop','wipe','type'],'enter'));
        optional(v.exit,x => oneOf(x,['none','fade','sink'],'exit')); optional(v.frames,x => integer(x,'animate.frames',1,36000)); });
    } else {
      oneOf(l.mode,['plain','karaoke'],'mode');
      check(Array.isArray(l.pages) && l.pages.length <= 10000,'captions: requires compiled pages');
      for (const p of l.pages) {
        object(p,['copy_id','from','to','lines'],'page'); string(p.copy_id,'copy_id',256); integer(p.from,'from'); integer(p.to,'to'); check(p.to > p.from, 'page: invalid interval');
        check(Array.isArray(p.lines) && p.lines.length > 0 && p.lines.length <= 100,'page.lines: invalid lines');
        for (const line of p.lines) {
          check(Array.isArray(line) && line.length <= 1024,'page.lines: too many words');
          for (const word of line) { object(word,['text','from','to'],'word'); string(word.text,'word',4096); integer(word.from,'from'); integer(word.to,'to'); }
        }
      }
    }
  }
}
export function validateJobs(jobs) {
  check(Array.isArray(jobs) && jobs.length > 0 && jobs.length <= LIMITS.jobs, 'jobs: expected 1..64 jobs');
  let frames = 0, pixels = 0;
  for (const job of jobs) {
    object(job, ['composition','props','out','stills','stills_dir','pid','renderer'], 'job');
    oneOf(job.composition, ['Scene'], 'composition');
    const renderer = job.renderer ?? job.props?.__renderer ?? 'browser';
    oneOf(renderer, Object.keys(CAPABILITIES), 'renderer');
    const p = job.props;
    object(p, ['width','height','fps','durationInFrames','offset','total','layers','__renderer','__environment'], 'props');
    optional(p.__renderer, v => check(v === renderer, 'renderer and props.__renderer must match'));
    if (p.__environment !== undefined) check(typeof p.__environment === 'string' && /^[a-f0-9]{64}$/.test(p.__environment), '__environment: expected a lowercase SHA-256 digest');
    integer(p.width,'width',1,LIMITS.dimension); integer(p.height,'height',1,LIMITS.dimension);
    check(p.width * p.height <= LIMITS.pixels, 'plate: pixel limit exceeded');
    num(p.fps,'fps',1,240); integer(p.durationInFrames,'durationInFrames',1,LIMITS.frames);
    optional(p.offset,v => integer(v,'offset')); optional(p.total,v => integer(v,'total',1));
    check((p.total ?? p.durationInFrames) >= (p.offset ?? 0) + p.durationInFrames, 'plate interval exceeds total');
    check(Array.isArray(p.layers) && p.layers.length <= LIMITS.layers, 'layers: too many layers');
    const ids = new Set();
    for (const l of p.layers) { layer(l,renderer); check(!ids.has(l.id), 'layer ids must be unique'); ids.add(l.id); }
    if (job.stills !== undefined) {
      check(Array.isArray(job.stills) && job.stills.length <= LIMITS.frames, 'stills: invalid frames');
      job.stills.forEach(f => integer(f,'still frame',0,p.durationInFrames - 1));
      string(job.stills_dir,'stills_dir',4096); check(isAbsolute(job.stills_dir), 'stills_dir must be absolute');
      string(job.pid,'pid',128); check(/^[A-Za-z0-9_-]+$/.test(job.pid), 'pid: unsafe output name');
    } else {
      string(job.out,'out',4096); check(isAbsolute(job.out) && job.out.endsWith('.mov'), 'out must be an absolute .mov path');
    }
    const count = job.stills?.length ?? p.durationInFrames;
    frames += count; pixels += count * p.width * p.height;
  }
  check(frames <= LIMITS.batchFrames && pixels <= LIMITS.pixelFrames, 'batch: frame/pixel budget exceeded');
  return jobs;
}
