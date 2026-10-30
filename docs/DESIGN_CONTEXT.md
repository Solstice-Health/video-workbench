# Versioned brand context for design

This increment adds local/project context packs, retrieval tools and structural
checks. It does not infer approvals or configure Solstice. Automatic commit
checks cover `vwb.canvas/2`; native After Effects projects require separate review.

## Workflow

1. Put explicitly authorized files in the workspace through attachments, local
   imports or an already configured integration.
2. Use `design_context_schema` for the exact JSON manifest schema. Classify
   sources and map enforceable requirements explicitly. Markdown is evidence,
   not executable harness instructions.
3. `design_context_import(path="brand/pack.json")` snapshots named source bytes.
   All source paths are relative to `work/`, even if the manifest is in a
   subdirectory. The returned SHA-256 identifies the immutable pack.
4. `design_context_resolve(pack=..., facts=...)` resolves scope and dates.
   Required rules come first; follow `next_offset` until every required rule
   has been read. Exact copy is never cut inside a rule.
5. `design_context_bind(output="board", pack=..., facts=...)` records the selected
   pack outside editable canvas JSON. Only the main role can bind it. Facts come
   from the person's brief; never guess missing scope or relax asset restrictions.
6. `design_context_read(pack=..., query="...")` searches source metadata by
   literal ID, title, kind or tags. Read a chosen source by `source` ID. Binary
   references become verified working copies for existing PDF/image/media tools.
7. `design_context_copy` mints pinned safety text into the copy deck;
   `design_context_asset` registers exact PNG/JPEG/WebP library bytes. Neither
   grants approval. For videos, read the source then use `media_register`.
8. Run `design_context_check` and inspect the preview. `canvas_check` and
   `canvas_commit` enforce required structural rules. The committed canvas and
   render receipt retain pack identity, brief facts and binding digest.

Packs imported in a project are shared across its conversations. Output bindings
remain conversation-local. Earlier private packs remain readable by their
original conversation after it joins a project; reimport from the project to
share them. Bindings are immutable per output: agent tools cannot replace the
pack or relax its brief facts. For a user-requested context change, create a new
output and bind the new context there; earlier outputs and versions keep theirs.

## Minimal manifest

This is a synthetic example, not medical guidance. `safety.md` contains exact
display copy. Use `lines: [start, end]` for a one-based inclusive text excerpt or
`html_id` for exactly one tagged HTML element. HTML is inert; scripts, styles,
templates and external resources are not executed. Check extracted text first.
Protected HTML requires explicit, balanced tags; ambiguous browser auto-closing
is rejected instead of guessed. Block boundaries remain separated in extracted text.

```json
{
  "schema_version": "vwb.design-context/1",
  "brand": "Example brand",
  "revision": "2026-10",
  "sources": [
    {
      "id": "isi", "path": "brand/safety.md", "title": "Safety display copy",
      "kind": "safety_information", "tags": ["isi", "patient"],
      "scope": {"audience": ["patient"], "market": ["US"]}
    },
    {
      "id": "logo", "path": "brand/logo.png", "title": "Library logo",
      "kind": "asset", "status": "approved", "approval_id": "source-record-123",
      "approval_evidence": ["proof"]
    },
    {
      "id": "proof", "path": "brand/proof.pdf", "title": "Prior proof",
      "kind": "example", "tags": ["prc", "reference"], "pages": [1, 3]
    },
    {
      "id": "guide", "path": "brand/rules.md", "title": "Design guidance",
      "kind": "guidelines", "tags": ["typography", "layout"]
    }
  ],
  "rules": [
    {
      "id": "required-isi", "kind": "required_copy", "level": "required",
      "description": "Use selected safety copy without paraphrasing",
      "sources": ["isi"], "when": {"audience": ["patient"]}, "min_frames": 90
    },
    {
      "id": "strict-library", "kind": "asset_allowlist", "level": "required",
      "description": "Only listed media when the brief requires strict assets",
      "sources": ["logo"], "when": {"asset_policy": ["strict"]}
    },
    {
      "id": "fonts", "kind": "fonts", "level": "required",
      "description": "Allowed typefaces", "values": ["Inter"]
    },
    {
      "id": "style", "kind": "guidance", "level": "advisory",
      "description": "Consult the guide and use generous spacing", "sources": ["guide"]
    }
  ]
}
```

Example facts: `{"audience":"patient","market":"US","asset_policy":"strict"}`.
Supported dimensions are audience, channel, market, locale, product and asset
policy (`strict`, `preferred`, `open`). Values are exact and case-sensitive.
Conditions mean AND across dimensions, OR within each dimension. Missing facts
are unknown; a known contradiction is false. Unknown applicability of a required
rule blocks binding/commit.

Sources declare scope, `valid_from`, `valid_until` and status. Resolution uses
the current UTC date and excludes retired/inapplicable evidence. Missing evidence
for a required rule blocks. Import a new revision explicitly; timestamps and
retrieval rank never silently supersede requirements. Checks of old versions
report current date validity while retaining their historical binding. This is
not a live upstream revocation feed.

## Rules and limits

| Rule | Deterministic check |
| --- | --- |
| `required_copy` | A text layer matches the selected source's words, case, punctuation and order; only whitespace is normalized. Reject obvious hidden/offscreen/too-short layers. |
| `asset_allowlist` | Consumed image, clip, vector or source-video bytes match one of the named source hashes; unfiltered rules also cover audio dependencies. |
| `required_asset` | The named asset hash is referenced. This differs from permitting only a library. |
| `fonts` | Text/caption layers declare an allowed family. |
| `palette` | Supported declared CSS colors normalize to the allowed hex values; unsupported syntax produces a finding. Does not inspect raster pixels or every interpolated color. |
| `min_font_size` | Requested size and allowed shrink floor meet `minimum`. |
| `guidance` | Advisory context, never an automatic pass/fail judgment. |

Rules may select layer `roles`; empty means all. Required rules accumulate.
Role-filtered media rules cover visual layers; empty-role media rules also cover
timeline audio. Bound asset references must pin an explicit revision. Media hashes
are read again before rendering; changing bytes after commit blocks a new render.
Required safety copy rejects entrance/exit effects that can alter its display.
Palette checks currently support CSS hex forms (`#RGB`, `#RGBA`, `#RRGGBB`,
`#RRGGBBAA`). Other declarations, including named colors and functions, require
an explicit supported equivalent rather than being silently ignored.
Disjoint font/palette/asset lists and incompatible required-asset/allowlist
combinations produce conflicts. There is no inferred override or supersession.
Advisories do not block. `preferred` does not secretly enable a strict allowlist.

Structural checks do not establish legibility, occlusion, full animation timing,
brand resemblance, clinical correctness, legal compliance or PRC approval.
Rasterized text and native `.aep` layers need separate inspection. Source approval
status, IDs and linked proofs are declared evidence, not an authenticated approval
service. A prior example never automatically authorizes new copy or an output.

Imports allow 128 sources/rules, 256 MiB/file and 512 MiB/pack. UTF-8 text sources
are at most 1 MiB; required copy totals at most 16,000 characters. Oversized copy
fails instead of being summarized. Source reads reject traversal, symlinks and
nonregular files. Raw and extracted bytes have separate hashes. Working inspection
copies are checked against originals before reuse. PDF page and video time anchors
are navigation hints; import does not validate OCR, transcripts or anchor existence.

## Research decisions

- **Mandatory coverage plus selective retrieval:** Anthropic describes upfront
  context combined with just-in-time exploration; its newer guidance emphasizes
  progressive disclosure. Keep required rules outside relevance top-k cutoffs and
  retrieve guidance/media selectively. This is an engineering choice to evaluate
  here, not a universal benchmark result.
  [Context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents),
  [July 2026 guidance](https://claude.dev/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models/).
- **Large context does not guarantee retrieval:** position-sensitive failures in
  *Lost in the Middle* motivate coverage tests. Those models/tasks do not establish
  current model rankings. [Paper](https://aclanthology.org/2024.tacl-1.9/).
- **Separate decisions from enforcement:** OPA documents this separation. A small
  typed evaluator fits this scope; brand rules stay separate from capability
  permissions in `config/policy.yaml`. [OPA](https://www.openpolicyagent.org/docs).
- **Keep provenance and selectors:** revision, attribution and derivation follow
  W3C PROV concepts; text/page/time locators follow Web Annotation concepts.
  Checksums establish integrity, not approval.
  [PROV-O](https://www.w3.org/TR/prov-o/),
  [Web Annotation](https://www.w3.org/TR/annotation-model/).
- **Measure evidence independently from quality:** ALCE distinguishes citation
  support and completeness. Measure required-rule coverage, exact-copy fidelity,
  prohibited substitutions, scope/revision accuracy, source traceability, false
  blocking, design quality, token use and latency separately.
  [ALCE](https://aclanthology.org/2023.emnlp-main.398/).

Each main-agent turn reintroduces binding references, including native resumes.
Exact copy remains in pinned sources rather than a summary. This supports
rediscovery; it does not prove model obedience.

## Solstice and next increments

Solstice remains a retrieval integration point. Its existing Workbench connection
manager/Registry tools can supply files after access is configured. The importer
accepts retrieved workspace files with an explicit `origin` citation; it does not
invent tool names, connect accounts or assume permissions.

A future adapter should retain connection/tool identity, arguments hash, upstream
revision/URI, retrieval time and response hash, then create the same local pack.
Retrieved content/annotations remain untrusted evidence.
[MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).

Further increments: human context-management UI; authenticated approval/revocation;
explicit supersession; DTCG token import; multimodal indexing; rendered visibility
checks; native-project checks. Add embeddings/reranking only after a measured miss.
Compare full eligible context with selective retrieval using identical hard checks;
separately ablate applicability filtering and context restoration. Current synthetic
tests are not a live model-quality or compliance benchmark.

## Validation recorded 2026-10-30

- **91 context tests passed**, covering tagged HTML boundaries, ambiguity rejection,
  immutable bindings, three-valued applicability, scope conflicts, paged mandatory
  coverage, exact copy, real consumed assets, explicit revisions, uncached byte
  verification, path boundaries and preflight checks before preview/full rendering.
- A user-supplied local brand snapshot was tested using six copied sources: tagged
  safety HTML, Markdown/JSON rules, source caveats and an original logo. The exact
  extracted copy contained 3,696 characters; logo bytes matched the source checksum.
  Unknown scope, hidden copy and an attempted strict-to-open policy change failed
  as intended. A three-frame browser render completed with the pack identity in
  its receipt. This short output tests rendering plumbing, not readable exposure
  time or publication suitability.
- Source approval/effective dates and patient safety text were unavailable in
  that snapshot; no approval was inferred. The source's requested fonts were not
  in the bundled library. The engineering render used a clearly labeled test
  font; production typography was not substituted or certified.
- These checks made no model calls, changed no original brand files and uploaded
  no brand material. Private fixtures and rendered outputs are outside Git.
