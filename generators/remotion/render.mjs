// Render a batch of overlay plates with alpha. Bundles once, renders each job in turn.
//   node render.mjs jobs.json
// jobs.json: [{ "composition": "Scene", "props": {...}, "out": "/abs/path/plate.mov" }]
// A job with "stills": [frames] and "stills_dir" renders only those frames as PNGs with alpha (previews).
import { bundle } from "@remotion/bundler";
import { renderMedia, renderStill, selectComposition } from "@remotion/renderer";
import { readFileSync, renameSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import os from "node:os";

const here = dirname(fileURLToPath(import.meta.url));
const jobs = JSON.parse(readFileSync(process.argv[2], "utf8"));
const concurrency = Math.max(2, Math.floor(os.cpus().length * 0.75));

const serveUrl = await bundle({ entryPoint: join(here, "src", "index.ts"), publicDir: join(here, "public") });
for (const job of jobs) {
  if (job.stills) {
    const t0 = Date.now();
    const composition = await selectComposition({ serveUrl, id: job.composition, inputProps: job.props });
    for (const frame of job.stills) {
      const out = join(job.stills_dir, `${job.pid}_f${frame}.png`);
      if (existsSync(out)) continue;
      await renderStill({ composition, serveUrl, output: out + ".part.png", frame, inputProps: job.props,
        imageFormat: "png", chromiumOptions: { gl: "angle" }, logLevel: "error" });
      renameSync(out + ".part.png", out);
    }
    console.log(JSON.stringify({ pid: job.pid, stills: job.stills.length, ms: Date.now() - t0 }));
    continue;
  }
  if (existsSync(job.out)) {
    console.log(JSON.stringify({ out: job.out, cached: true }));
    continue;
  }
  const t0 = Date.now();
  const composition = await selectComposition({ serveUrl, id: job.composition, inputProps: job.props });
  const tmp = job.out + ".part.mov";
  await renderMedia({
    composition,
    serveUrl,
    codec: "prores",
    proResProfile: "4444",
    pixelFormat: "yuva444p10le",
    imageFormat: "png",
    outputLocation: tmp,
    inputProps: job.props,
    concurrency,
    chromiumOptions: { gl: "angle" },
    logLevel: "error",
  });
  renameSync(tmp, job.out);
  console.log(JSON.stringify({ out: job.out, frames: composition.durationInFrames, ms: Date.now() - t0 }));
}
