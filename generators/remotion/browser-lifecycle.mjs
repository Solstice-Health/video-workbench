import { chromium } from 'playwright-core';

export const SHUTDOWN_GRACE_MS = 5000;
export function deadline(promise, ms, label) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), ms);
  })]).finally(() => clearTimeout(timer));
}

export async function launchOwnedBrowser(executablePath, signal) {
  const server = await chromium.launchServer({ executablePath, host: '127.0.0.1', headless: true,
    timeout: SHUTDOWN_GRACE_MS, chromiumSandbox: true,
    // This module owns shutdown; Playwright's signal handlers can otherwise wait
    // forever for a stuck browser's graceful-close protocol response.
    handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false,
    env: { PATH: process.env.PATH ?? '', HOME: process.env.HOME ?? '', TMPDIR: process.env.TMPDIR ?? '/tmp', LANG: 'en_US.UTF-8' },
    args: ['--disable-background-networking', '--disable-component-update', '--disable-sync', '--no-first-run',
      '--disable-default-apps', '--disable-domain-reliability', '--force-color-profile=srgb',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'] });
  let closing;
  const close = () => closing ??= (async () => {
    try { await deadline(server.close(), SHUTDOWN_GRACE_MS, 'Chromium shutdown'); }
    catch {
      // Public Playwright API kills its exact owned process group, including
      // renderer/GPU children, and removes its temporary profile. No pkill or
      // process-name searches, and no unrelated browser processes are touched.
      await deadline(server.kill(), SHUTDOWN_GRACE_MS, 'Chromium forced shutdown');
    } finally { signal?.removeEventListener('abort', abort); }
  })();
  const abort = () => { void close().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    signal?.throwIfAborted();
    const browser = await chromium.connect(server.wsEndpoint(), { timeout: SHUTDOWN_GRACE_MS });
    signal?.throwIfAborted();
    return { browser, process: server.process(), close };
  } catch (error) { await close(); throw error; }
}
