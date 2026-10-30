# After Effects and Blender

Workbench can drive a native creative application as well as its existing video
compositor. After Effects uses ExtendScript and `aerender`, without an MCP server.
Blender uses a pinned local MCP bridge. Neither application is included in the
Python package, and an available tool is not proof that its application is open.

Open **Plugins and MCPs → Manage connections** to set up either application. The
permission settings apply to this Workbench host, across its conversations.
Selecting the connection in a message tells the agent which route you want; it
does not enable automation or grant additional authority.

Native JSX and Blender Python run as your desktop user, with local file and
network access. They are not Docker scripts. The controls keep the execution
permission disabled initially, and the agent cannot enable it through a tool.
Keep projects backed up and enable execution only for trusted work. Provider API
keys are not passed to the bridge processes. A native application may still have
its own signed-in accounts and filesystem access.

## After Effects

This implementation supports the macOS application. Install and sign in to a
licensed Adobe After Effects, then:

1. In After Effects, open **Settings → Scripting & Expressions** and enable
   **Allow Scripts To Write Files And Access Network**. Completion receipts and
   saved projects need file writing.
2. In Workbench's After Effects connection, choose **Check installation**, read
   the host-access notice and enable automation.
3. On the first run, allow the operating system's Automation request to control
   After Effects. Workbench does not change these system permissions itself.
4. Select After Effects in the composer and ask the agent to build or edit a
   composition. Its native tools are available to the main agent and creative
   helpers; no extra MCP server or Adobe credentials in `.env` are required.

The tool sequence is `after_effects_status`, write a `.jsx` with `file_write`,
`after_effects_run`, then `after_effects_job` to inspect the result. Scripts can
create compositions, layers, text, shapes, keyframes and expressions, import
footage, and access the rest of the installed application's scripting API.
Plugins, fonts and effects still need to exist in that installation.

### Native authoring target

The Canvas picker can select `after_effects` as a **native project** target.
The editable source is an `.aep` in the workspace and the native AE application
is the authoring surface. Workbench can display the rendered movie or still; it
does not embed AE's interactive editor. There is no automatic conversion of an
arbitrary `vwb.canvas/2` document, and `canvas_set_renderer` cannot select AE.
Moving a rendered movie into a canvas preserves the rendered pixels, not AE's
editable layers, effects, expressions or keyframes. Retain the `.aep` to edit them.

`canvas_renderers` and the catalog endpoint retain their four `renderers` rows
and add `targets`. Each target has `id`, `label`, `kind`, `description`, `scope`,
`capability`, `required_tools` and `supports_renderer_swap`. The four adapter IDs
have `kind: "canvas_renderer"`; `after_effects` has `kind: "native_project"` and
also supplies an ordered `workflow` and `import_tools: ["media_register"]`.
Its `availability` is `{status, reason}`, with status `unknown`, `unavailable`,
`setup_required` or `available`. The base catalog does not probe the host. A host
caller can supply `catalog(after_effects_status=...)`; the `after_effects_status`
tool also returns this metadata under `target`.

Availability describes installation and configuration, not an authority grant
or a live execution test. Even `available` cannot verify the Adobe license,
scripting preference or macOS consent without running a job. Choosing the target
conveys authoring intent only. Calls still pass through Registry role permissions,
the `desktop.after_effects.execute` policy and the human-enabled connection.
Existing enabled configuration is reused; selecting AE does not request a second
blanket approval or change desktop settings. A configured policy of `ask` or
`block` still applies. No new tool or policy capability is required by this target.

### Author, render and import

An example tool sequence (paths are relative to `work/`):

1. Call `after_effects_status`. Resolve any reported installation or enablement
   requirement through the human settings flow above.
2. Use `file_write` to save `native/build.jsx`. A minimal example below creates
   an editable composition and reports installed template names rather than
   assuming an output format is available:

   ```javascript
   var comp = app.project.items.addComp("Main", 640, 360, 1, 1, 24);
   comp.layers.addText("Native composition");
   var queued = app.project.renderQueue.items.add(comp);
   vwb.result = {
       comp: comp.name,
       render_queue_index: queued.index,
       render_templates: queued.templates,
       output_templates: queued.outputModule(1).templates
   };
   ```

3. Call `after_effects_run({"script_path":"native/build.jsx"})`, then
   `after_effects_job({"job_id":"<returned id>","wait_s":30})` until terminal.
   Continue only on `completed`. Retain `project.path`, its SHA-256 and the
   returned composition/template metadata. Later edits can pass this path as
   `project_path` to another isolated script job to create a new saved copy.
4. Call `after_effects_render` with `project_path` from the successful script,
   `comp: "Main"` (or `render_queue_index`), a new `output_path` such as
   `native/render-v1.mov`, and a compatible installed `output_module` template
   selected from the returned names. The template controls the format/codec;
   changing the filename extension does not configure it. Poll the render job.
5. A completed MOV/MP4 job returns `output.path`, a file hash and an `import_hint`
   containing the optional `media_register` call. Invoke that call separately to
   snapshot the movie as an immutable source. It remains subject to `media.register`
   policy; the job lead can import a composer's render. Inspect it using
   `media_frame(target="source:<returned ref>", frame=0)`. Run `index_build`
   before workflows that need source indexing. Importing does not convert the
   native composition into a canvas or commit an output.

Still images remain workspace files and can be inspected with `image_inspect`.
Native AVI/MXF and audio outputs are not directly importable by `media_register`;
use a compatible MOV/MP4 render for video reuse. Every native job result/receipt
identifies `authoring_target: "after_effects"` and `review.status: "not_evaluated"`.
Completion checks files, not design fidelity or delivery acceptance. Native
projects/renders require explicit review; JSON canvas checks run only if a
separate canvas is authored and committed.

`after_effects_status` returns the absolute host `workspace_path`. Within a
script, `vwb.workPath`, `vwb.outputPath` and `vwb.projectPath` give the exact paths
on the Mac, rather than the agent container's `/work`. `vwb.log(text)` records
progress and `vwb.result` can return JSON-compatible metadata. The agent can
declare `expected_outputs` so a job fails if its promised files are absent.

By default a script creates a separate project under
`work/after-effects/<job-id>/project.aep`. Workbench refuses to discard unsaved
changes in the current project. A workspace `project_path` can initialize a new
copy; `project_mode="current"` explicitly modifies the current GUI project and
does not automatically save it. The generated script and runner remain in the
workspace for examination. Script execution is serialized across conversations.

`after_effects_render` launches the installed `aerender` against a saved workspace
project and a composition or render-queue entry. Outputs must be new workspace
files. The job validates that a successful renderer produced its output; output
module and render-setting names must match the local installation. Movie, audio
and single-frame outputs are supported; image-sequence patterns are not.

Jobs run independently of a chat turn and have durable status and bounded logs.
Stopping a chat does not cancel them. `after_effects_job(cancel=true)` stops only
an owned renderer or script waiter, never the user's entire GUI application.
Stopping the AppleScript waiter cannot guarantee that JSX stopped inside After
Effects. A timed-out or cancelled script therefore blocks another script until
its completion receipt arrives or you verify it stopped and clear the interrupted
execution in Manage connections. Press Escape in After Effects if needed. An
interrupted job is never automatically replayed.

If automatic discovery misses a nonstandard installation, the host configuration
endpoint accepts an `app_path` and `aerender_path`; the application must identify
itself as Adobe After Effects. These are human settings, not agent tool arguments.
An installed app still needs its license, scripting preference and macOS consent.

### Adobe evidence and validation limits

Primary documentation checked 2026-10-30:

- [Adobe Scripts](https://helpx.adobe.com/after-effects/desktop/automate-in-after-effects/automate-animation/scripts.html)
  documents JSX authoring, disabled file/network writes by default, Escape to
  stop scripts, and Windows `afterfx -r` dispatch into the existing application.
  This bridge implements macOS AppleScript dispatch; it does not claim Windows
  execution or use `aerender` as a general JSX runner.
- [Adobe automated rendering](https://helpx.adobe.com/after-effects/desktop/render-and-export/automate-rendering/automated-rendering-network-rendering.html)
  documents saved-project/comp or queue selection, installed template names,
  inclusive end frames and a separate application instance unless `-reuse` is
  requested. The bridge omits `-reuse` and does not relax missing-footage checks.
  Adobe supports image sequences; this bridge deliberately accepts only movies,
  audio and a single still with equal start/end frames. Fonts, plugins and encoders
  must be present on the rendering host.

The development host inspection found no AE app in `/Applications` or
`~/Applications`, and no saved AE enablement in this checkout. Tests use a
synthetic installation and process/JSX host to exercise the native project,
render and import route. They also decode a real generated movie after import.
This is integration evidence for Workbench, not proof of native Adobe execution,
visual equivalence, installed templates or macOS Automation consent. A licensed
macOS AE host is still required for those checks; no preferences are changed by
the tests or catalog discovery.

## Blender MCP

This preset uses the third-party **MCP for Blender**, formerly `blender-mcp`,
version **2.2.2**. It is distinct from Blender Lab's experimental first-party MCP
server. The selected bridge supports Blender 3.0 and later and uses an MCP stdio
process to talk to the add-on's loopback socket.

1. Install Blender. Open Workbench's Blender MCP connection and choose
   **Prepare Blender add-on**. Preparation verifies the release wheel's SHA-256
   and installs hash-locked dependencies into a private runtime. It does not run
   the upstream automatic setup script, change other MCP clients, or edit Blender
   preferences.
2. Copy the displayed add-on path. In Blender, use **Edit → Preferences → Add-ons
   → Install from Disk**, select `addon.py`, and enable **MCP for Blender**.
3. Open the add-on's sidebar panel with **N** in the 3D viewport. Start its local
   server on loopback, using port **9876** by default. Keep Blender open. Only one
   client should control that add-on instance at a time.
4. Choose **Connect Blender** in Workbench. A connection is only marked verified
   after scene inspection succeeds, rather than after the stdio server starts.
5. Enable **Blender Python execution** separately to let the agent create, edit,
   animate, save and render. Save a separate `.blend` before changing existing work.

Select Blender MCP in the composer. The agent discovers exact names and schemas
with `integration_tools(integration="blender_mcp")`. The catalog supplies the
absolute host workspace path for saving outputs. `integration_call` exposes
curated local inspection tools, including scene information and the `look`
viewport tool. With the human Python permission enabled,
`integration_execute` exposes `blender_mcp.execute_blender_code`. Blender's `bpy`
API provides general scene construction and rendering; this is not a fixed set
of scene templates.

Embedded viewport images are validated and imported into
`work/integrations/blender_mcp/` with provenance and reusable raster asset refs.
Save project files, exported geometry and renders under the supplied workspace
path so the existing workspace and canvas can open supported media. Native
`.blend` projects remain downloadable files; Workbench does not embed Blender's
interactive 3D editor inside its canvas.

The bridge has a private process home and omits provider credentials. Upstream
telemetry is disabled. This preset excludes cloud asset downloads and paid 3D
generation tools; it does not configure those services on your behalf. Calls are
serialized, bounded and not automatically replayed. An interrupted Python edit
may already have changed Blender: inspect the scene before retrying it. Disconnect
revokes new Workbench calls; it does not guarantee an already-dispatched script
stopped inside Blender.

## Runtime and source references

After Effects settings live in `runs/.desktop/after-effects/`. Private job state
lives under the request's `.after-effects/` directory; generated scripts and
projects remain under `work/after-effects/`. Blender connection state and its
isolated runtime live in `runs/.integrations/blender_mcp/`. These directories are
ignored by Git and are not included in a source ZIP. A new host must install the
applications and repeat the connection setup.

- [Adobe's scripting documentation](https://helpx.adobe.com/after-effects/desktop/automate-in-after-effects/automate-animation/scripts.html)
- [MCP for Blender source and setup](https://github.com/ahujasid/mcp-for-blender)
- [Pinned bridge release](https://pypi.org/project/mcp-for-blender/2.2.2/)
- [Blender Lab's separate experimental MCP server](https://www.blender.org/lab/mcp-server/)
