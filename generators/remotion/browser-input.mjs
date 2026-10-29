import { constants } from 'node:fs';
import { lstat, open } from 'node:fs/promises';

// Pin the inode before reading. O_NONBLOCK prevents a swapped-in FIFO from
// blocking open; O_NOFOLLOW rejects a swapped-in symlink. fstat is authoritative.
export async function readBoundedRegularFile(path, maxBytes = 16 * 1024 * 1024) {
  const before = await lstat(path);
  if (!before.isFile()) throw new Error('Input must be a regular file, not a symlink, FIFO or device');
  if (before.size > maxBytes) throw new Error(`Input exceeds ${maxBytes} bytes`);
  if (constants.O_NOFOLLOW === undefined || constants.O_NONBLOCK === undefined) {
    throw new Error('This platform cannot safely open bounded input files');
  }
  const fd = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const initial = await fd.stat();
    if (!initial.isFile() || initial.dev !== before.dev || initial.ino !== before.ino) {
      throw new Error('Input must remain the same regular file while opening');
    }
    if (initial.size > maxBytes) throw new Error(`Input exceeds ${maxBytes} bytes`);
    const chunks = [];
    let total = 0;
    for (;;) {
      // Read one extra byte to detect a file that grows after fstat, with bounded
      // allocation even if a concurrent writer continuously appends to it.
      const buffer = Buffer.allocUnsafe(Math.min(65536, maxBytes + 1 - total));
      const { bytesRead } = await fd.read(buffer, 0, buffer.length, total);
      if (!bytesRead) break;
      total += bytesRead;
      if (total > maxBytes) throw new Error(`Input exceeds ${maxBytes} bytes`);
      chunks.push(buffer.subarray(0, bytesRead));
    }
    const after = await fd.stat();
    if (initial.size !== after.size || initial.mtimeMs !== after.mtimeMs || initial.ctimeMs !== after.ctimeMs || total !== initial.size) {
      throw new Error('Input changed while reading');
    }
    return Buffer.concat(chunks, total);
  } finally { await fd.close(); }
}

export async function readJobsFile(path, maxBytes) {
  return JSON.parse((await readBoundedRegularFile(path, maxBytes)).toString('utf8'));
}
