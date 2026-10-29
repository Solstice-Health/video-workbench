import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, open, rm, symlink, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { readJobsFile } from '../browser-input.mjs';

test('jobs reader accepts bounded regular JSON, rejecting symlinks, directories and oversized files', async () => {
  const dir=await mkdtemp(join(tmpdir(),'vwb-input-'));
  try {
    const path=join(dir,'jobs.json');
    await writeFile(path,'[{"key":"value"}]');
    assert.deepEqual(await readJobsFile(path),[{key:'value'}]);
    assert.deepEqual(await readJobsFile(path,17),[{key:'value'}]);
    await assert.rejects(readJobsFile(path,16),/exceeds/);
    await symlink(path,join(dir,'linked.json'));
    await assert.rejects(readJobsFile(join(dir,'linked.json')),/regular file/);
    await assert.rejects(readJobsFile(dir),/regular file/);
    const fd=await open(join(dir,'large.json'),'w');
    try { await fd.truncate(16*1024*1024+1); } finally { await fd.close(); }
    await assert.rejects(readJobsFile(join(dir,'large.json')),/exceeds/);
    await writeFile(path,'{bad json');
    await assert.rejects(readJobsFile(path),SyntaxError);
  } finally { await rm(dir,{recursive:true,force:true}); }
});

test('CLI rejects a FIFO without opening it or blocking for a writer', {skip:process.platform==='win32',timeout:10000}, async () => {
  const dir=await mkdtemp(join(tmpdir(),'vwb-fifo-'));
  try {
    const path=join(dir,'jobs.pipe');
    execFileSync('mkfifo',[path]);
    await assert.rejects(readJobsFile(path),/regular file/);
    assert.throws(() => execFileSync(process.execPath,[fileURLToPath(new URL('../browser-render.mjs',import.meta.url)),path],
      {timeout:3000,stdio:'pipe'}), error => {
      assert.equal(error.status,1,'must exit itself, without the test deadline killing it');
      assert.match(error.stderr.toString(),/regular file/);
      return true;
    });
  } finally { await rm(dir,{recursive:true,force:true}); }
});
