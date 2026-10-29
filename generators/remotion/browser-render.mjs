// node browser-render.mjs jobs.json
// Only repository-owned, bundled entrypoints run. Props cross as structured data.
import { build } from 'esbuild';
import { createServer } from 'node:http';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFile, mkdir, mkdtemp, rename, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { LIMITS, validateJobs, RENDERER_PINS } from './browser-validation.mjs';
import { findChromium, findFFmpeg, fingerprintEnvironment, rendererEnvironment } from './browser-environment.mjs';
import { readJobsFile } from './browser-input.mjs';
import { deadline, launchOwnedBrowser, SHUTDOWN_GRACE_MS } from './browser-lifecycle.mjs';
export { LIMITS, validateJobs, RENDERER_PINS } from './browser-validation.mjs';
export { findChromium, rendererEnvironment } from './browser-environment.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const children = new Set();

export async function createTrustedServer() {
  const result = await build({ entryPoints: [join(here, 'src', 'browser-entry.tsx')], bundle: true, write: false,
    platform: 'browser', format: 'iife', target: 'chrome120', define: { 'process.env.NODE_ENV': '"production"' },
    minify: true, sourcemap: false, metafile: true, logLevel: 'silent' });
  if (Object.keys(result.metafile.inputs).some(p => /node_modules\/(?:remotion|@remotion)\//.test(p))) throw new Error('Standalone bundle must not import Remotion');
  const prefix = `/${randomBytes(24).toString('hex')}/`;
  const resources = new Map();
  const html = '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:transparent;overflow:hidden}*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}#plate{position:absolute;inset:0}</style></head><body><div id="plate"></div><script src="app.js"></script></body></html>';
  resources.set(prefix, { type: 'text/html; charset=utf-8', data: Buffer.from(html) });
  resources.set(`${prefix}app.js`, { type: 'text/javascript; charset=utf-8', data: Buffer.from(result.outputFiles[0].contents) });
  const manifest = JSON.parse(await readFile(join(here, 'public', 'fonts', 'manifest.json'), 'utf8'));
  for (const family of manifest) for (const face of family.faces) {
    if (!/^[a-z0-9-]+\.woff2$/.test(face.file)) throw new Error('Invalid trusted font manifest');
    resources.set(`${prefix}fonts/${face.file}`, { type: 'font/woff2', path: join(here, 'public', 'fonts', face.file) });
  }
  const server = createServer(async (req, res) => {
    const resource = resources.get(req.url);
    if (req.method !== 'GET' || !resource || req.headers.host !== `127.0.0.1:${server.address().port}`) { res.writeHead(404); res.end(); return; }
    try {
      const data = resource.data ?? await readFile(resource.path);
      res.writeHead(200, { 'Content-Type': resource.type, 'Content-Length': data.length,
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; font-src 'self'; connect-src 'none'; img-src 'none'; media-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" });
      res.end(data);
    } catch { res.writeHead(500); res.end(); }
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  await new Promise((yes,no) => { server.once('error',no); server.listen(0,'127.0.0.1',yes); });
  const origin = `http://127.0.0.1:${server.address().port}`;
  return { url: `${origin}${prefix}`, allowed: new Set([...resources.keys()].map(path => origin + path)),
    close: () => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }) };
}

export async function createPage(browser, server, job) {
  const context = await browser.newContext({ viewport: { width: job.props.width, height: job.props.height }, deviceScaleFactor: 1,
    serviceWorkers: 'block', acceptDownloads: false, locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce' });
  context.setDefaultTimeout(LIMITS.frameMs);
  let navigated = false;
  await context.route('**/*', async route => {
    const request = route.request();
    if (!server.allowed.has(request.url()) || request.method() !== 'GET') return route.abort('blockedbyclient');
    if (request.isNavigationRequest()) {
      if (navigated || request.url() !== server.url || request.frame().parentFrame()) return route.abort('blockedbyclient');
      navigated = true;
    }
    return route.continue();
  });
  await context.routeWebSocket('**/*', socket => socket.close());
  const page = await context.newPage();
  page.on('popup', popup => { void popup.close(); });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(server.url, { waitUntil: 'load', timeout: LIMITS.frameMs });
    await deadline(page.evaluate(async ({ props, renderer }) => window.__vwb.initialize(props, renderer), {
      props: job.props, renderer: job.renderer ?? job.props.__renderer ?? 'browser',
    }), LIMITS.frameMs, 'Font/scene initialization');
    return { page, context, errors };
  } catch (error) {
    await deadline(context.close(), SHUTDOWN_GRACE_MS, 'Context shutdown').catch(() => {});
    throw error;
  }
}
export async function captureFrame(session, frame) {
  await deadline(session.page.evaluate(frame => window.__vwb.frame(frame), frame), LIMITS.frameMs, 'Frame evaluation');
  if (session.errors.length) throw new Error(`Browser render error: ${session.errors[0]}`);
  const png = await session.page.screenshot({ type: 'png', omitBackground: true, animations: 'disabled', caret: 'hide', timeout: LIMITS.frameMs });
  // Chromium encodes fully opaque screenshots as RGB, even with omitBackground.
  // Transparent frames retain RGBA; the Python cache normalizes opaque PNGs.
  if (![2, 6].includes(png[25])) throw new Error('Screenshot must be an RGB or RGBA PNG');
  return png;
}

function processRun(command, args, { input = false, timeout = LIMITS.frameMs } = {}) {
  // No shell and no vendor credentials in subprocess environments.
  const env = { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '', LANG: 'C' };
  const child = spawn(command, args, { stdio: [input ? 'pipe' : 'ignore', 'pipe', 'pipe'], env });
  children.add(child);
  let stderr = '', stdout = '', expired = false;
  child.stderr.on('data', data => { stderr = (stderr + data).slice(-16384); });
  child.stdout.on('data', data => { stdout = (stdout + data).slice(-65536); });
  // EPIPE is reported by write callbacks or by the completion promise, never unhandled.
  child.stdin?.on('error', () => {});
  const timer = setTimeout(() => { expired = true; child.kill('SIGKILL'); }, timeout);
  const done = new Promise((yes, no) => {
    child.once('error', no);
    child.once('close', code => code === 0 && !expired ? yes(stdout) : no(new Error(`${command} ${expired ? 'timed out' : `exited ${code}`}: ${stderr}`)));
  }).finally(() => { clearTimeout(timer); children.delete(child); });
  done.catch(() => {});
  const ready = new Promise((yes, no) => { child.once('spawn', yes); child.once('error', no); });
  ready.catch(() => {});
  return { child, done, ready };
}
async function verifyVideo(path, props) {
  const result = await processRun('ffprobe', ['-v','error','-count_frames','-select_streams','v:0','-show_entries',
    'stream=codec_name,pix_fmt,width,height,nb_read_frames,avg_frame_rate','-of','json',path]).done;
  const stream = JSON.parse(result).streams?.[0];
  const rate = stream?.avg_frame_rate?.split('/').map(Number);
  if (stream?.codec_name !== 'prores' || !stream.pix_fmt.startsWith('yuva444p') || Number(stream.nb_read_frames) !== props.durationInFrames ||
      stream.width !== props.width || stream.height !== props.height || !rate || Math.abs(rate[0] / rate[1] - props.fps) > 0.0001) {
    throw new Error(`Rendered video failed ProRes alpha/frame-count/dimension/fps postconditions: ${result}`);
  }
}
async function renderJob(browser, server, job, signal, cancel, ffmpegPath) {
  const t0 = Date.now(), session = await deadline(createPage(browser, server, job), LIMITS.frameMs, 'Page initialization');
  const dest = job.stills !== undefined ? job.stills_dir : dirname(job.out);
  let temp, encoder, expired = false;
  const stop = () => { expired = true; encoder?.child.kill('SIGKILL'); };
  signal.addEventListener('abort', stop, { once: true });
  const timer = setTimeout(cancel, LIMITS.jobMs);
  const check = () => { if (expired || signal.aborted) throw new Error('Render cancelled or timed out'); };
  try {
    check(); await mkdir(dest,{recursive:true}); temp = await mkdtemp(join(dest,'.vwb-render-'));
    if (job.stills !== undefined) {
      const { writeFile } = await import('node:fs/promises');
      const names = new Set();
      for (const frame of job.stills) {
        check(); const png = await captureFrame(session,frame); check();
        const name = `${job.pid}_f${frame}.png`; names.add(name); await writeFile(join(temp,name),png,{flag:'w'});
      }
      check();
      for (const name of names) { check(); await rename(join(temp,name),join(dest,name)); }
      return { pid: job.pid, stills: job.stills.length, ms: Date.now() - t0 };
    }
    const tmp = join(temp,'plate.mov');
    encoder = processRun(ffmpegPath,['-hide_banner','-loglevel','error','-y','-f','image2pipe','-framerate',String(job.props.fps),
      '-vcodec','png','-i','pipe:0','-an','-c:v','prores_ks','-profile:v','4444','-pix_fmt','yuva444p10le',
      '-alpha_bits','16','-threads','2','-frames:v',String(job.props.durationInFrames),tmp], { input:true, timeout:LIMITS.jobMs });
    await encoder.ready;
    for (let frame = 0; frame < job.props.durationInFrames; frame++) {
      check(); const png = await captureFrame(session,frame); check();
      await deadline(new Promise((yes,no) => encoder.child.stdin.write(png, error => error ? no(error) : yes())), LIMITS.frameMs, 'Encoder input');
    }
    encoder.child.stdin.end(); await encoder.done;
    await verifyVideo(tmp,job.props); check(); await rename(tmp,job.out);
    return { out:job.out,frames:job.props.durationInFrames,ms:Date.now() - t0 };
  } finally {
    clearTimeout(timer); signal.removeEventListener('abort',stop);
    encoder?.child.kill('SIGKILL'); if (encoder) await encoder.done.catch(() => {});
    try { await deadline(session.context.close(), SHUTDOWN_GRACE_MS, 'Context shutdown'); }
    finally { if (temp) await rm(temp,{recursive:true,force:true}); }
  }
}

export async function renderJobs(jobs, { onResult = result => console.log(JSON.stringify(result)) } = {}) {
  validateJobs(jobs);
  const executablePath = await findChromium();
  let ffmpegPath = 'ffmpeg';
  if (jobs.some(job => job.props.__environment !== undefined)) {
    ffmpegPath = await findFFmpeg();
    const environment = await fingerprintEnvironment({ chromiumPath: executablePath, ffmpegPath });
    for (const job of jobs) {
      if (job.props.__environment !== undefined && job.props.__environment !== environment.sha256) {
        throw new Error('Renderer environment changed; expected props.__environment to match the current environment SHA-256');
      }
    }
  }
  const controller = new AbortController();
  let owned, server;
  const cancel = () => { controller.abort(); for (const child of children) child.kill('SIGKILL'); };
  const timer = setTimeout(cancel,LIMITS.batchMs);
  process.once('SIGINT',cancel); process.once('SIGTERM',cancel); process.once('SIGHUP',cancel);
  try {
    server = await createTrustedServer();
    controller.signal.throwIfAborted();
    owned = await launchOwnedBrowser(executablePath, controller.signal);
    for (const job of jobs) {
      controller.signal.throwIfAborted();
      onResult(await renderJob(owned.browser,server,job,controller.signal,cancel,ffmpegPath));
    }
  } finally {
    clearTimeout(timer);
    for (const child of children) child.kill('SIGKILL');
    try { await owned?.close(); }
    finally {
      try { await server?.close(); }
      finally { process.removeListener('SIGINT',cancel); process.removeListener('SIGTERM',cancel); process.removeListener('SIGHUP',cancel); }
    }
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 3) throw new Error('Usage: node browser-render.mjs <jobs.json> | --environment');
    if (process.argv[2] === '--environment') console.log(JSON.stringify(await rendererEnvironment()));
    else await renderJobs(await readJobsFile(process.argv[2], LIMITS.jsonBytes));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
