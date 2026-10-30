# Set up and run Video Workbench

The complete application lives in [Solstice-Health/video-workbench](https://github.com/Solstice-Health/video-workbench).
Use branch `codex/feat-adaptable-canvas` for this setup.
The Python backend serves the bundled React/Eeva frontend on the same origin.
There is one version to install and one server to start. No separate frontend
build, frontend development server or CORS configuration is needed. Node is used
by the video renderer and frontend tests.

## Choose the host

Use macOS on Apple Silicon for the complete video workflow. The source-video
index currently requires MLX Whisper and Apple Vision. Linux can run the chat UI,
native API agents, workspace previews, Docker sandbox and renderer, but is not a
drop-in replacement for the full source-video indexing pipeline. Windows is not
currently a supported host; the setup script and process controls use POSIX APIs.

The control-plane service binds to loopback and stores provider keys, sign-ins and
workspaces on that host. Use an SSH tunnel for private remote access, or the
protected public mode below for a trusted group's shared instance. Public mode
requires a private launch link/access code and a session cookie. Everyone admitted
shares the host's conversations, files, settings and integrations; there are no
per-user accounts, permissions or workspace isolation.

## Install prerequisites

Install Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), Node
22 or 24 with npm, and FFmpeg (including ffprobe). uv creates the pinned Python
3.12 environment. Docker Desktop must be running for Codex agents and sandbox
automation. Chrome is needed for the shared headed browser.

On a Mac with Homebrew:

```bash
brew install uv ffmpeg node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"
```

Install and open Docker Desktop separately. Installing dependencies does not
start a model call, a browser session or a video job.

## Clone and set up

```bash
git clone --branch codex/feat-adaptable-canvas --single-branch git@github.com:Solstice-Health/video-workbench.git
cd video-workbench
./scripts/setup.sh --with-agent --with-browser
```

The script syncs Python from `uv.lock`, runs `npm ci` from the renderer lockfile,
copies the pinned fonts, installs Remotion's render browser, builds
`vwb-codex-agent:0.162.1`, installs the checksum-pinned shared browser driver, and
runs readiness checks. It never overwrites an existing `.env`, including one in
a parent directory, and refuses to change dependencies while this checkout's
Workbench server is running.

The optional capabilities can be installed later:

```bash
./scripts/setup.sh                 # Python, bundled UI and video renderer
./scripts/setup.sh --ui-only       # Python and bundled UI; skip renderer setup
./scripts/setup.sh --with-agent    # also build the Docker agent/sandbox image
./scripts/setup.sh --with-browser  # also install the shared browser driver
./scripts/setup.sh --with-code-tools # also build read-only rg/Git/Python/TypeScript tools
```

`--ui-only` is useful for reviewing the application before configuring media
dependencies. It does not make rendering, video indexing or Docker automation
available. Python dependencies are still installed from the same locked project.

For repository work, `uv run python -m vwb.code.runtime install` can install the
pinned code-tools image independently. It needs Docker, not a host npm install.
Search, Git and language-server tools share this image across model providers;
normal media/chat use does not require it. See [code intelligence](CODE_INTELLIGENCE.md)
for supported operations, saved-file context, bounds and evaluation.

## Configure providers and choose a model

If no environment file exists, setup creates a private `.env` template. Add the
keys needed for your workflow, or open **model picker → Models and APIs → Provider
APIs** after starting the UI. Never commit a real `.env` or copy `runs/.keys` into
a deployment image.

- **OpenAI**: default Astra/Codex main agent, GPT helpers and image generation.
  Codex execution also requires the Docker image.
- **Anthropic**: direct Opus/Fable main agents. Select one in the model picker
  before sending a message if you configure only this provider. No Claude desktop
  login substitutes for this API key. Sandbox scripts still require Docker.
- **Google**: video-native reading and Omni video generation. Full source indexing
  also uses the other configured readers and local Apple Silicon tools.
- **OpenRouter**: its model catalog and supported multimodal requests, plus
  optional public web search, fetch and bounded research tools.

Agents can discover and use these APIs through `model_providers`, `model_catalog`
and `model_request`. Scripts use `sandbox_run(models=true)` and the credential-free
`vwb_models` helper. Google also accepts `GEMINI_API_KEY` when `GOOGLE_API_KEY` is
unset. See [model access from tools and scripts](MODEL_ACCESS.md) for discovery,
supported operations and generated-file handling.

Keys are resolved from `VWB_ENV_FILE`, or the nearest `.env` at or above the source
checkout. Provider overrides saved through the UI take precedence and live in
the host's private `runs/.keys` store. The browser and agent containers do not
receive provider keys. See [models](MODELS.md), [browser setup](BROWSER.md) and
[MCP/Figma connections](INTEGRATIONS.md) for capability-specific setup.

That same file also supports the allowlisted public host settings
`VWB_UI_PUBLIC_URL`, `VWB_SOLSTICE_MCP_URL`, `VWB_SOLSTICE_CLIENT_ID` and
`VWB_SOLSTICE_REDIRECT_URI`. For these four settings, an explicit shell/service
value (including an empty string) overrides `.env`; Solstice configuration saved
in Manage connections takes precedence over those defaults. Values are literal,
without variable interpolation. Workbench reads only the allowlisted host settings;
do not source or bulk-export `.env`. See the
[optional Solstice settings example](INTEGRATIONS.md#required-deployment-and-one-time-sign-in).

To select explicit host paths, export them in the shell before starting the
server; these path selectors are process settings, not `.env` host settings:

```bash
export VWB_ENV_FILE=/absolute/private/path/workbench.env
export VWB_RUNS_DIR=/absolute/persistent/path/workbench-runs
```

Keep `VWB_RUNS_DIR` the same on every start. It contains conversations, original
uploads, generated assets, outputs, approvals, provider sessions and sign-ins.
Allow substantial free disk space for lossless video plates and renders.

## Start frontend and backend

```bash
.venv/bin/vwb doctor
.venv/bin/vwb ui --no-open --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). `vwb ui` without `--no-open`
opens the browser automatically. Loading the page does not launch an agent.
Choose the model, context budget, effort and agent prompt in the composer. The
default prompt is Descriptive; Minimal, Loose and three industry-inspired
profiles are also available.

For local mode when `.env` contains a public origin, launch with
`VWB_UI_PUBLIC_URL= .venv/bin/vwb ui --no-open --port 8765`. This explicit empty
override disables public mode for that process. Select the separately registered
local Solstice callback in `.env` or Manage connections before connecting locally.

Optional native creative applications are configured in **Plugins and MCPs →
Manage connections**. After Effects uses macOS ExtendScript and `aerender`;
Blender MCP prepares its own pinned bridge and matching add-on. Neither desktop
application is installed by the setup script. See [desktop application setup](DESKTOP_APPS.md)
for installation, host execution permissions, outputs and cancellation behavior.

For a UI-only installation, check readiness with `.venv/bin/vwb doctor --ui-only`.
Missing optional providers or Docker are reported without printing key values.

For a remote host, keep the backend bound to loopback and forward the port:

```bash
ssh -N -L 8765:127.0.0.1:8765 your-user@your-workbench-host
```

Open the same localhost URL on your computer. Keep the tunnel running. The
backend's integrations and files belong to the remote host. A Figma desktop MCP
connection needs Figma on the control-plane host; it does not automatically reach
your laptop's desktop app through this tunnel.

For a protected shared HTTPS instance, choose one launch method:

```bash
# Host-installed/configured ngrok, using your reserved stable hostname:
.venv/bin/vwb ui --ngrok --public-url https://workbench.example --no-open --port 8765
# Or an existing HTTPS reverse proxy forwarding to 127.0.0.1:8765:
.venv/bin/vwb ui --public-url https://workbench.example --no-open --port 8765
```

Replace the placeholder hostname. `--public-url` overrides `VWB_UI_PUBLIC_URL`
from the shell or `.env`; public startup pins the canonical origin in the server
environment for callback validation. Open the printed private launch link.
Register and configure the exact public Solstice callback separately; setting the
origin does not fill it in. See [public access](PUBLIC_ACCESS.md) for proxy Host
headers, temporary ngrok URLs, URL ownership and shutdown, and
[integration setup](INTEGRATIONS.md#required-deployment-and-one-time-sign-in) for
the pre-registered public/native Auth0 client, PKCE and sign-in requirements.
An organization-approved client can serve approved installations with each exact
callback allowlisted; a separate Client ID per teammate is not required. No
default Client ID ships with Workbench, and grants stay local to each host.

## Verify an installation without spending model tokens

```bash
uv run --frozen pytest -q
node generators/remotion/scripts/check-workbench-ui.mjs
uv build
```

Node must be on PATH for the frontend regression tests. CI checks this explicitly
instead of silently skipping them. The HTTP and provider tests use fixtures and
local test servers; they do not run paid model jobs. The distribution smoke check
also tests a built wheel outside the source checkout; CI documents the exact
command in `.github/workflows/ci.yml`.

The source checkout is the supported full-workflow setup. Wheels bundle the UI,
config, recipes, schema, prompts and renderer sources, but do not bundle FFmpeg,
Node packages, render-browser downloads, Docker or the shared browser binary.

## Update and roll back

1. Let active turns finish, or choose **Stop** and wait for the worker to exit.
   Background sandbox jobs survive chat Stop; keep their files and inspect them
   before replacing dependencies. A server restart interrupts its current main
   worker, so this build does not promise zero-downtime model turns.
2. Stop the UI server with Ctrl-C. Keep the same environment file and runs folder.
3. Record `git rev-parse HEAD`, then update this feature branch:

   ```bash
   git fetch origin codex/feat-adaptable-canvas
   git switch codex/feat-adaptable-canvas
   git pull --ff-only origin codex/feat-adaptable-canvas
   ./scripts/setup.sh --with-agent --with-browser
   ```

   Reuse the setup options chosen for your installation, such as `--ui-only`.
   Python and renderer lockfiles update together with the UI and backend.
4. Restart with the same local or public launch command and reload the browser once.
   Queued messages remain paused after a restart. **Resume turn** continues an
   interrupted turn; **Resume queued messages** releases its pending queue.

For a code rollback, stop the service, check out the recorded commit and rerun
setup before restarting. Retain your environment and workspaces. A code checkout
does not undo files or external actions produced by an agent. Back up the runs
folder before upgrades that change persisted formats; this project does not yet
provide automatic workspace migrations or database downgrade guarantees.

## Source, data and the older frontend snapshot

Push code, lockfiles, local UI assets, scripts and documentation together. `.env`,
OAuth grants, provider overrides, browser profiles, conversations, uploaded media,
renders, caches, virtual environments and `node_modules` remain local and ignored.
Moving to a new machine requires a separate private data transfer and fresh
sign-ins where required; a Git clone intentionally does not contain them.
A repository push does not register an Auth0 client or deploy the upstream
Solstice gateway/Figma extension. Those are separate administrator actions.

`Solstice-Health/VideoHarnessFrontend` is an older frontend-only export. Use the
frontend bundled with this repository's backend for matching APIs. Deploying that
separate snapshot over a newer backend can restore bugs or incompatible controls.
