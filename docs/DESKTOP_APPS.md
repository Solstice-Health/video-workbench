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
