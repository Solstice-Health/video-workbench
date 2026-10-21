# Eeva browser assets

Source of truth: [the organization Eeva artifact](https://claude.ai/artifact/Duj6yyXvM9NpMGedFMsKKg).
Artifact metadata identifies `Solstice-Health/Solstice-Frontend`, source revision
`25c18f2d7` (`ercan/sol-3600-intake-leave-guard`), synced on 2026-10-07.
Assets were prepared for Video Workbench on 2026-10-12.

## Contents and provenance

- `README.md`, `tokens.json`, `design-system.json`, component documentation,
  `components/index.d.ts`, and `components/bundle.css` came from the existing
  local artifact export. The CSS has one adaptation: its remote Spectral import
  points to the local `spectral.css` instead.
- `tokens.css` resolves all 167 artifact token aliases and adds the documented
  typography properties and Aileron font declarations. Load this **after**
  `components/bundle.css`: that CSS contains source token mappings which otherwise
  refer to undefined non-prefixed variables.
- `fonts/aileron-{100,300,400,600,700,800}.woff2` were copied from the organization's
  existing `Solstice-Frontend/public/fonts/aileron` checkout. No 500 face was invented.
  `fonts/Aileron-NOTICE.txt` records the author's redistribution terms and source.
- `spectral.css` and `fonts/spectral-300-0.ttf` were cached from the Google Fonts
  Spectral 300 stylesheet. `fonts/Spectral-OFL.txt` retains the upstream license.
- `assets/Logos/solstice-mark.svg` and `solstice-mark-ember.svg` use the exact filled
  path in the source revision's SolsticeMark component, colored with the documented
  ink and ember tokens.
- `source/` contains the 32 original design-system TS/TSX modules fetched via
  GitHub at `25c18f2d7`. No component source was changed.
- `components/bundle.js` is a reconstructed browser bundle of those source modules,
  **not a downloaded copy of the artifact's original compiled bundle**. The artifact's
  original JS was absent from the local export. Existing Radix, Lucide, clsx, and
  tailwind-merge packages from the organization's local frontend checkout were used.
  Their exact versions are recorded in `bundle-dependencies.json`; their available
  license texts are preserved under `licenses/`.

The distribution audit on 2026-10-21 added notices for 11 dependencies whose
published packages omitted a LICENSE file. Each exact version in
`bundle-dependencies.json` was checked for its MIT declaration and repository.
The ten Radix notices use the WorkOS license from
https://github.com/radix-ui/primitives/blob/main/LICENSE;
`react-remove-scroll-bar` uses its upstream license from
https://github.com/theKashey/react-remove-scroll-bar/blob/master/LICENSE.
All 47 listed dependencies now have their own notice under `licenses/`.

The bundle was built with the existing native esbuild executable under
`generators/remotion/node_modules/@esbuild/darwin-arm64/bin/esbuild`:
`--bundle --format=iife --global-name=Eeva --minify --platform=browser --target=es2020`.
JSX uses `React.createElement` and `React.Fragment`. The organization `cn` helper
was reproduced as `twMerge(clsx(inputs))`. Browser shims map `react` and `react-dom`
to `window.React` and `window.ReactDOM`; `react/jsx-runtime` delegates to
`React.createElement`. React is not duplicated in the Eeva bundle.

## Runtime loading order

1. `components/bundle.css`
2. `tokens.css`
3. `../react.production.min.js`
4. `../react-dom.production.min.js`
5. `components/bundle.js`
6. Application code (optionally importing `../htm.mjs`)

The adjacent React and ReactDOM files are version-pinned upstream browser UMD
builds from unpkg, version 18.3.1. The adjacent `htm.mjs` is htm 3.1.1.
Their license files sit alongside them. All browser code and fonts are local.

Validation: both React globals and all 33 Eeva component exports load in a JS VM;
every `--ds-*` variable referenced in the component CSS has a declaration in the
combined stylesheets. `checksums.json` records SHA-256 values of the vendored files.
