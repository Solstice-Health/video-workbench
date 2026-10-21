// Copy the pinned font library (every @fontsource package in package.json) into public/fonts and write
// public/fonts/manifest.json, so renders never fetch fonts from the network and the harness can tell agents
// exactly which families, weights and styles exist. No family is a default: the canvas names its font.
import { copyFileSync, mkdirSync, existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const out = join(root, "public", "fonts");
mkdirSync(out, { recursive: true });
mkdirSync(join(out, "licenses"), { recursive: true });
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
const ids = Object.keys(pkg.dependencies).filter((d) => d.startsWith("@fontsource/")).sort();

const manifest = [];
let copied = 0;
for (const id of ids) {
  const dir = join(root, "node_modules", id);
  const meta = JSON.parse(readFileSync(join(dir, "metadata.json"), "utf8"));
  const version = JSON.parse(readFileSync(join(dir, "package.json"), "utf8")).version;
  const license = ["LICENSE", "LICENSE.txt", "OFL.txt", "OFL.md"].find((name) => existsSync(join(dir, name)));
  if (!license) throw new Error(`no redistribution license found for ${id}`);
  const licenseFile = `licenses/${meta.id}.txt`;
  copyFileSync(join(dir, license), join(out, licenseFile));
  const entry = { family: meta.family, id: meta.id, version, license: meta.license?.type ?? "OFL-1.1",
    license_file: licenseFile, category: meta.category, faces: [] };
  for (const style of ["normal", "italic"]) {
    for (const w of meta.weights) {
      if (w < 300) continue;
      if (style === "italic" && w !== 400 && w !== 700) continue;
      const file = `${meta.id}-latin-${w}-${style}.woff2`;
      const src = join(dir, "files", file);
      if (!existsSync(src)) continue;
      copyFileSync(src, join(out, file));
      entry.faces.push({ weight: w, style, file });
      copied += 1;
    }
  }
  if (!entry.faces.length) throw new Error(`no latin faces found for ${id}`);
  manifest.push(entry);
}
// Remove files of packages that are no longer listed.
const keep = new Set(manifest.flatMap((m) => m.faces.map((f) => f.file)));
for (const f of readdirSync(out)) if (f.endsWith(".woff2") && !keep.has(f)) console.log(`stale font file ${f} (not in manifest)`);
writeFileSync(join(out, "manifest.json"), JSON.stringify(manifest, null, 1) + "\n");
console.log(`copied ${copied} font files for ${manifest.length} families to ${out}`);
