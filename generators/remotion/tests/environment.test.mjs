import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { canonicalJSON, fingerprintEnvironment } from '../browser-environment.mjs';
import { props } from './fixtures.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const root=fileURLToPath(new URL('../',import.meta.url));

test('environment hashes actual dependency pins, font manifest/bytes, adapter sources and binaries without paths', async () => {
  const dir=await mkdtemp(join(tmpdir(),'vwb-env-'));
  const fixture=join(dir,'original');
  try {
    await mkdir(join(fixture,'public','fonts'),{recursive:true});
    await mkdir(join(fixture,'src'),{recursive:true});
    const files={
      'package-lock.json':'{"packages":{"react":{"version":"19.3.0"}}}',
      'public/fonts/manifest.json':'[{"family":"Fixture","faces":[{"file":"fixture.woff2"}]}]',
      'public/fonts/fixture.woff2':'actual font bytes',
      'browser-render.mjs':'trusted adapter source',
      'browser-environment.mjs':'the fingerprint source hashes itself',
      'src/browser-entry.tsx':'trusted entry source',
      'src/animation.ts':'trusted animation source',
      'chromium':'actual chrome bytes', 'ffmpeg':'actual encoder bytes',
    };
    for (const [name,bytes] of Object.entries(files)) await writeFile(join(fixture,name),bytes);
    const fingerprint = base => fingerprintEnvironment({root:base,chromiumPath:join(base,'chromium'),ffmpegPath:join(base,'ffmpeg')});
    const initial=await fingerprint(fixture);
    assert.equal(initial.sha256,hash(JSON.stringify(Object.fromEntries(Object.entries(initial.details).sort(([a],[b]) => a<b?-1:1)))));
    assert.equal(initial.details.chromium_sha256,hash(files.chromium));
    assert.equal(initial.details.ffmpeg_sha256,hash(files.ffmpeg));
    assert.equal(initial.details.package_lock_sha256,hash(files['package-lock.json']));
    assert.deepEqual(Object.keys(initial.details).sort(),['adapter_sources_sha256','arch','chromium_sha256','ffmpeg_sha256','fonts_sha256','os_release','package_lock_sha256','platform']);
    assert.ok(!JSON.stringify(initial).includes(dir));
    assert.deepEqual(initial,await fingerprint(fixture));
    const relocated=join(dir,'different-absolute-location');
    await cp(fixture,relocated,{recursive:true});
    assert.deepEqual(initial,await fingerprint(relocated),'relocation cannot change cache identity');
    for (const [name,bytes] of Object.entries(files)) {
      const changed=name==='public/fonts/manifest.json' ? bytes.replace('Fixture','Fixture renamed') : `${bytes}\n`;
      await writeFile(join(fixture,name),changed);
      assert.notEqual((await fingerprint(fixture)).sha256,initial.sha256,`${name} must partition the cache`);
      await writeFile(join(fixture,name),bytes);
    }
    await writeFile(join(fixture,'browser-new-helper.mjs'),'new trusted helper');
    assert.notEqual((await fingerprint(fixture)).sha256,initial.sha256,'new adapter modules enter identity automatically');
    assert.equal(canonicalJSON({z:'é',a:{y:'漢',b:'text'}}),'{"a":{"b":"text","y":"漢"},"z":"é"}');
  } finally { await rm(dir,{recursive:true,force:true}); }
});

test('environment CLI only hashes binaries and rejects a stale pin before browser launch or output writes', {timeout:30000}, async () => {
  const dir=await mkdtemp(join(tmpdir(),'vwb-env-cli-'));
  try {
    // These executable fixtures intentionally fail if launched. Successful
    // inspection proves neither Chromium nor ffmpeg is executed by this CLI.
    const chrome=join(dir,'chromium'), ffmpeg=join(dir,'ffmpeg');
    for (const path of [chrome,ffmpeg]) await writeFile(path,'#!/bin/sh\necho "BINARY_WAS_LAUNCHED" >&2\nexit 97\n',{mode:0o755});
    const env={...process.env,PATH:dir,VWB_CHROMIUM_EXECUTABLE:chrome};
    const run = args => execFileSync(process.execPath,[join(root,'browser-render.mjs'),...args],{env,timeout:10000,encoding:'utf8',stdio:'pipe'});
    const environment=JSON.parse(run(['--environment']));
    assert.equal(environment.sha256,hash(canonicalJSON(environment.details)));
    assert.equal(environment.details.chromium_sha256,hash(await readFile(chrome)));
    assert.equal(environment.details.ffmpeg_sha256,hash(await readFile(ffmpeg)));
    assert.deepEqual(environment,JSON.parse(run(['--environment'])));
    const p=props(); p.__environment='0'.repeat(64);
    const out=join(dir,'existing.mov'); await writeFile(out,'keep existing');
    const jobs=join(dir,'jobs.json');
    await writeFile(jobs,JSON.stringify([{composition:'Scene',renderer:'browser',props:p,out}]));
    assert.throws(() => run([jobs]), error => {
      assert.equal(error.status,1);
      assert.match(error.stderr,/Renderer environment changed/);
      assert.doesNotMatch(error.stderr,/BINARY_WAS_LAUNCHED/);
      return true;
    });
    assert.equal(await readFile(out,'utf8'),'keep existing');
  } finally { await rm(dir,{recursive:true,force:true}); }
});
