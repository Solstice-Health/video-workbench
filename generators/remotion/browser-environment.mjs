import { chromium } from 'playwright-core';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { access, open, realpath, readdir, stat } from 'node:fs/promises';
import { arch, platform, release } from 'node:os';
import { delimiter, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readBoundedRegularFile } from './browser-input.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
// Matches Python canonical_json(sort_keys=True, separators=(',', ':'),
// ensure_ascii=False). Environment details contain only string values.
export function canonicalJSON(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

async function executable(path) {
  try { await access(path, constants.X_OK); return (await stat(path)).isFile(); } catch { return false; }
}
export async function findChromium() {
  if (process.env.VWB_CHROMIUM_EXECUTABLE) {
    const path = process.env.VWB_CHROMIUM_EXECUTABLE;
    if (!await executable(path)) throw new Error('VWB_CHROMIUM_EXECUTABLE is not an executable file');
    return realpath(path);
  }
  // Reuse only an already-downloaded binary, without importing Remotion.
  async function scan(dir, depth = 0) {
    if (depth > 4) return;
    let entries; try { entries = await readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const item of entries.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
      const path = join(dir, item.name);
      if (item.isFile() && /^chrome-headless-shell(?:\.exe)?$/.test(item.name) && await executable(path)) return realpath(path);
      if (item.isDirectory()) { const found = await scan(path, depth + 1); if (found) return found; }
    }
  }
  const downloaded = await scan(join(here, 'node_modules', '.remotion', 'chrome-headless-shell'));
  if (downloaded) return downloaded;
  const installed = chromium.executablePath();
  if (await executable(installed)) return realpath(installed);
  throw new Error('Chromium is not installed. Set VWB_CHROMIUM_EXECUTABLE to an existing binary, or explicitly run npm run setup-browser. Rendering never downloads browsers.');
}
export async function findFFmpeg() {
  for (const dir of (process.env.PATH ?? '').split(delimiter)) {
    const path = resolve(dir || '.', process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg');
    if (await executable(path)) return realpath(path);
  }
  throw new Error('ffmpeg is not installed on PATH');
}

async function hashFile(path) {
  // Stream large executables, rejecting replacement or mutation during hashing.
  // Resolve trusted installation symlinks (e.g. Homebrew) before pinning the fd.
  const fd = await open(await realpath(path), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const before = await fd.stat();
    if (!before.isFile() || before.size > 2 ** 31) throw new Error('Environment input must be a regular file smaller than 2 GiB');
    const hash = createHash('sha256'), buffer = Buffer.allocUnsafe(1024 * 1024);
    let total = 0;
    for (;;) {
      const { bytesRead } = await fd.read(buffer, 0, Math.min(buffer.length, before.size + 1 - total), total);
      if (!bytesRead) break;
      total += bytesRead;
      if (total > before.size) throw new Error('Environment input changed while hashing');
      hash.update(buffer.subarray(0, bytesRead));
    }
    const after = await fd.stat();
    if (total !== before.size || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) {
      throw new Error('Environment input changed while hashing');
    }
    return hash.digest('hex');
  } finally { await fd.close(); }
}

async function sourceFiles(root) {
  const files = (await readdir(root)).filter(name => /^browser.*\.mjs$/.test(name));
  // Include all trusted TypeScript sources, conservatively including shared
  // modules even when a future entrypoint changes its import graph.
  async function visit(relative) {
    for (const item of await readdir(join(root, relative), { withFileTypes: true })) {
      const name = `${relative}/${item.name}`;
      if (item.isDirectory()) await visit(name);
      else if (/\.tsx?$/.test(item.name)) files.push(name);
    }
  }
  await visit('src');
  return files.sort();
}

// Paths are trusted host configuration, never accepted through job props. The
// helper permits isolated fixture trees in tests; paths never enter the digest.
export async function fingerprintEnvironment({ root = here, chromiumPath, ffmpegPath }) {
  const fonts = join(root, 'public', 'fonts');
  const manifestBytes = await readBoundedRegularFile(join(fonts, 'manifest.json'));
  const manifest = JSON.parse(manifestBytes.toString('utf8'));
  const names = new Set();
  for (const family of manifest) for (const face of family.faces) {
    if (!/^[a-z0-9-]+\.woff2$/.test(face.file)) throw new Error('Invalid trusted font manifest');
    names.add(face.file);
  }
  const fontHashes = [['manifest.json', digest(manifestBytes)]];
  for (const name of [...names].sort()) fontHashes.push([name, await hashFile(join(fonts, name))]);
  const sourceHashes = [];
  for (const name of await sourceFiles(root)) sourceHashes.push([name, await hashFile(join(root, name))]);
  const details = {
    adapter_sources_sha256: digest(canonicalJSON(sourceHashes)),
    package_lock_sha256: await hashFile(join(root, 'package-lock.json')),
    fonts_sha256: digest(canonicalJSON(fontHashes)),
    chromium_sha256: await hashFile(chromiumPath),
    ffmpeg_sha256: await hashFile(ffmpegPath),
    platform: platform(), arch: arch(), os_release: release(),
  };
  return { sha256: digest(canonicalJSON(details)), details };
}

export async function rendererEnvironment() {
  return fingerprintEnvironment({ chromiumPath: await findChromium(), ffmpegPath: await findFFmpeg() });
}
