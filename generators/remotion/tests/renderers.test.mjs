import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, readdir, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';
import { chromium } from 'playwright-core';
import { createTrustedServer, createPage, captureFrame, findChromium, rendererEnvironment, renderJobs } from '../browser-render.mjs';
import { props } from './fixtures.mjs';
const root=dirname(dirname(fileURLToPath(import.meta.url)));
const enabled=process.env.VWB_RENDER_SMOKE === '1';
const probe=path => JSON.parse(execFileSync('ffprobe',['-v','error','-count_frames','-show_entries','stream=codec_name,pix_fmt,nb_read_frames,width,height','-of','json',path],{encoding:'utf8'})).streams[0];

test('real browser/GSAP/Canvas2D nonconsecutive seeking, RGBA and network isolation', {skip:!enabled,timeout:180000}, async () => {
  const server=await createTrustedServer();
  const browser=await chromium.launch({executablePath:await findChromium(),headless:true});
  try {
    for (const renderer of ['browser','gsap','canvas2d']) {
      const job={composition:'Scene',renderer,props:props(renderer!=='canvas2d'),out:'/tmp/unused.mov'};
      const session=await createPage(browser,server,job);
      try {
        const first=await captureFrame(session,1);
        const later=await captureFrame(session,3);
        await new Promise(resolve=>setTimeout(resolve,100));
        const again=await captureFrame(session,1);
        assert.deepEqual(first,again,`${renderer} must repeat exact PNG bytes after backward seeking`);
        assert.notDeepEqual(first,later,`${renderer} must animate`);
        assert.equal(first[25],6,'RGBA PNG');
        const shifted = { ...job, props: { ...job.props, offset: 0, durationInFrames: 14 } };
        const shiftedSession = await createPage(browser, server, shifted);
        try { assert.deepEqual(first, await captureFrame(shiftedSession, 11), renderer + ': offset must map to absolute frame'); }
        finally { await shiftedSession.context.close(); }
        if (renderer !== 'canvas2d') assert.ok(await session.page.evaluate(() => document.fonts.status === 'loaded' && document.fonts.size > 0));
        assert.equal(await session.page.evaluate(() => document.querySelectorAll('img').length),0);
        assert.equal(await session.page.evaluate(async () => { try {await fetch('https://example.com/');return false;} catch{return true;} }),true);
        assert.equal(await session.page.evaluate(async () => { try {await fetch(new URL('missing',location.href));return false;} catch{return true;} }),true);
        await assert.rejects(session.page.goto(`${server.url}app.js`));
      } finally {await session.context.close();}
    }
    assert.equal((await fetch(`${server.url}../../package.json`)).status,404);
  } finally {await browser.close();await server.close();}
});

test('opaque full-background PNGs render and publish across browser/GSAP/Canvas2D', {skip:!enabled,timeout:120000}, async t => {
  const dir=await mkdtemp(join(tmpdir(),'vwb-opaque-tests-'));
  let server,browser;
  const assertOpaque = (png, p, label) => {
    // Chromium can omit the alpha channel when every pixel is opaque. Either
    // encoding is valid; the installed Chrome exercises the optimized RGB case.
    assert.ok([2,6].includes(png[25]),`${label}: expected RGB or RGBA PNG`);
    const rgba=execFileSync('ffmpeg',['-v','error','-f','image2pipe','-vcodec','png','-i','pipe:0',
      '-frames:v','1','-pix_fmt','rgba','-f','rawvideo','pipe:1'],
      {input:png,timeout:15000,maxBuffer:p.width*p.height*4+8192});
    assert.equal(rgba.length,p.width*p.height*4,`${label}: decoded dimensions`);
    for (let i=3;i<rgba.length;i+=4) assert.equal(rgba[i],255,`${label}: alpha at pixel ${(i-3)/4}`);
    assert.deepEqual([...rgba.subarray(-4)],[17,34,51,255],`${label}: uncovered background pixel`);
  };
  try {
    server=await createTrustedServer();
    browser=await chromium.launch({executablePath:await findChromium(),headless:true});
    const jobs=[];
    for (const renderer of ['browser','gsap','canvas2d']) {
      const p=props(); p.__renderer=renderer;
      p.layers.unshift({id:'opaque-bg',type:'shape',shape:'rect',box:[0,0,p.width,p.height],fill:'#112233'});
      const job={composition:'Scene',renderer,props:p,stills:[1],stills_dir:dir,pid:`opaque-${renderer}`};
      jobs.push(job);
      const session=await createPage(browser,server,job);
      try {
        const first=await captureFrame(session,1);
        const later=await captureFrame(session,3);
        assertOpaque(first,p,renderer);
        assertOpaque(later,p,renderer);
        assert.notDeepEqual(first,later,`${renderer}: foreground still animates over opaque background`);
        assert.deepEqual(first,await captureFrame(session,1),`${renderer}: opaque backward seek repeats`);
        t.diagnostic(`${renderer}: opaque PNG color type ${first[25]} (${first[25]===2?'RGB optimized':'RGBA'})`);
      } finally {await session.context.close();}
    }
    // Exercise publication too; Python may then normalize RGB files to RGBA.
    await renderJobs(jobs,{onResult:()=>{}});
    for (const job of jobs) assertOpaque(await readFile(join(dir,`${job.pid}_f1.png`)),job.props,`${job.renderer} published still`);
  } finally {
    await browser?.close();
    await server?.close();
    await rm(dir,{recursive:true,force:true});
  }
});

test('real CLI stills and exact-count alpha ProRes for every alternate backend; cleanup on encode failure', {skip:!enabled,timeout:240000}, async () => {
  const dir=await mkdtemp(join(tmpdir(),'vwb-render-tests-'));
  try {
    const jobs=[];
    const environment=await rendererEnvironment();
    for (const renderer of ['browser','gsap','canvas2d']) {
      const p=props(renderer!=='canvas2d'); p.__renderer=renderer; p.__environment=environment.sha256;
      jobs.push({composition:'Scene',renderer,props:p,stills:[2,0,2],stills_dir:dir,pid:renderer,out:join(dir,`${renderer}.mov`)});
      jobs.push({composition:'Scene',renderer,props:p,out:join(dir,`${renderer}.mov`)});
    }
    await writeFile(join(dir,'jobs.json'),JSON.stringify(jobs));
    // Exercise the exact public command Python invokes, with no runtime installs.
    const { spawn }=await import('node:child_process');
    const child=spawn(process.execPath,[join(root,'browser-render.mjs'),join(dir,'jobs.json')],{env:process.env,stdio:['ignore','pipe','pipe']});
    let error=''; child.stderr.on('data',d=>error+=d); child.stdout.resume();
    assert.equal(await new Promise((yes,no)=>{child.on('error',no);child.on('close',yes);}),0,error);
    for (const renderer of ['browser','gsap','canvas2d']) {
      const png=await readFile(join(dir,`${renderer}_f2.png`)); assert.equal(png[25],6);
      const s=probe(join(dir,`${renderer}.mov`)); assert.equal(s.codec_name,'prores'); assert.match(s.pix_fmt,/^yuva444p/); assert.equal(s.nb_read_frames,'4');
      // Decode alpha and check both untouched transparency and rendered nonzero opacity.
      const alpha=execFileSync('ffmpeg',['-v','error','-i',join(dir,`${renderer}.mov`),'-vf','alphaextract','-frames:v','1','-pix_fmt','gray','-f','rawvideo','pipe:1']);
      assert.ok(alpha.includes(0)); assert.ok(alpha.some(x=>x>100));
    }
    const originalPath=process.env.PATH;
    process.env.PATH=dir;
    const out=join(dir,'existing.mov'); await writeFile(out,'keep existing');
    try { await assert.rejects(renderJobs([{composition:'Scene',renderer:'browser',props:props(),out}],{onResult:()=>{}}),/ENOENT/); }
    finally {process.env.PATH=originalPath;}
    assert.equal(await readFile(out,'utf8'),'keep existing');
    assert.ok(!(await readdir(dir)).some(n=>n.startsWith('.vwb-render-')));
  } finally {await rm(dir,{recursive:true,force:true});}
});

test('Remotion adapter still renders original Scene contract and alpha video', {skip:!enabled,timeout:240000}, async () => {
  const {bundle}=await import('@remotion/bundler');
  const {renderStill,renderMedia,selectComposition}=await import('@remotion/renderer');
  const dir=await mkdtemp(join(tmpdir(),'vwb-remotion-tests-'));
  const browserExecutable=await findChromium();
  try {
    const serveUrl=await bundle({entryPoint:join(root,'src/index.ts'),publicDir:join(root,'public'),outDir:join(dir,'bundle')});
    const inputProps=props(true);
    const composition=await selectComposition({serveUrl,id:'Scene',inputProps,browserExecutable});
    for (const [name,frame] of [['a',1],['b',3],['c',1]]) await renderStill({serveUrl,composition,inputProps,frame,browserExecutable,output:join(dir,`${name}.png`),imageFormat:'png',logLevel:'error'});
    assert.deepEqual(await readFile(join(dir,'a.png')),await readFile(join(dir,'c.png')));
    assert.notDeepEqual(await readFile(join(dir,'a.png')),await readFile(join(dir,'b.png')));
    await renderMedia({serveUrl,composition,inputProps,browserExecutable,outputLocation:join(dir,'remotion.mov'),codec:'prores',proResProfile:'4444',pixelFormat:'yuva444p10le',imageFormat:'png',concurrency:1,logLevel:'error'});
    const s=probe(join(dir,'remotion.mov')); assert.equal(s.nb_read_frames,'4'); assert.match(s.pix_fmt,/^yuva444p/);
  } finally {await rm(dir,{recursive:true,force:true});}
});
