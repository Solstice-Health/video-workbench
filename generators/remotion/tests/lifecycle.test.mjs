import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { findChromium } from '../browser-environment.mjs';
import { deadline, launchOwnedBrowser, SHUTDOWN_GRACE_MS } from '../browser-lifecycle.mjs';
import { props } from './fixtures.mjs';

const enabled=process.env.VWB_RENDER_SMOKE==='1' && process.platform!=='win32';
const root=fileURLToPath(new URL('../',import.meta.url));

test('SIGTERM force-kills only the owned stopped Chromium and cleans encoder/staging within the shutdown bound',
  {skip:!enabled,timeout:45000}, async () => {
    const dir=await mkdtemp(join(tmpdir(),'vwb-signal-'));
    let child, control, childDone;
    try {
      control=await launchOwnedBrowser(await findChromium());
      const p=props(); p.durationInFrames=36000; p.total=36010;
      const out=join(dir,'existing.mov'); await writeFile(out,'keep existing');
      const jobs=join(dir,'jobs.json');
      await writeFile(jobs,JSON.stringify([{composition:'Scene',renderer:'browser',props:p,out}]));
      // Instrument the public launchServer API in this test child only. The
      // actual renderJobs signal handler, encoder and shutdown path run intact.
      const script=`
        import { chromium } from 'playwright-core';
        import { renderJobs } from './browser-render.mjs';
        import { readJobsFile } from './browser-input.mjs';
        const launch=chromium.launchServer.bind(chromium);
        chromium.launchServer=async options => {
          const server=await launch(options);
          process.send({chromiumPid:server.process().pid});
          return server;
        };
        try { await renderJobs(await readJobsFile(process.argv[1])); }
        catch (error) { console.error(error.message); process.exitCode=1; }
        finally { process.disconnect(); }
      `;
      child=spawn(process.execPath,['--input-type=module','-e',script,jobs],
        {cwd:root,env:process.env,stdio:['ignore','pipe','pipe','ipc']});
      let stderr=''; child.stderr.on('data',data => {stderr+=data;}); child.stdout.resume();
      childDone=once(child,'close');
      const [{chromiumPid}]=await deadline(once(child,'message'),10000,'Test Chromium launch');
      const started=Date.now();
      while (!(await readdir(dir)).some(name=>name.startsWith('.vwb-render-'))) {
        assert.ok(Date.now()-started<10000,'render must reach its staging directory');
        await new Promise(resolve=>setTimeout(resolve,20));
      }
      process.kill(chromiumPid,'SIGSTOP');
      const stopped=Date.now();
      child.kill('SIGTERM');
      const [code,signal]=await deadline(childDone,SHUTDOWN_GRACE_MS*2+3000,'Test renderer termination');
      assert.equal(code,1,stderr); assert.equal(signal,null,'Node must exit through its own cleanup');
      assert.ok(Date.now()-stopped>=SHUTDOWN_GRACE_MS-300,'stopped Chrome must exercise the forced-kill fallback');
      assert.throws(() => process.kill(chromiumPid,0),{code:'ESRCH'},'owned Chromium must be gone');
      assert.equal(await readFile(out,'utf8'),'keep existing','cancelled output cannot replace a prior artifact');
      assert.ok(!(await readdir(dir)).some(name=>name.startsWith('.vwb-render-')),'staging must be removed after cancellation');
      const page=await control.browser.newPage();
      assert.equal(await page.evaluate(()=>2+2),4,'independent browser must remain alive');
    } finally {
      child?.kill('SIGKILL');
      if (childDone) await deadline(childDone,5000,'Test child cleanup').catch(()=>{});
      await control?.close();
      await rm(dir,{recursive:true,force:true});
    }
  });
