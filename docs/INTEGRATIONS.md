# Direct MCP servers and provider integrations

The built-in **Blender MCP** preset uses a pinned stdio bridge to the local Blender
add-on. **After Effects** uses native macOS scripting tools and does not require
MCP. Both appear in Manage connections, with separate human controls for native
code execution. Their setup and lifecycle are described in
[desktop applications](DESKTOP_APPS.md).

## Connect an MCP server directly

Solstice is optional. In **Plugins → Manage connections → Add MCP server**, enter
a name and its Streamable HTTP endpoint. Automatic authentication connects public
servers immediately or opens the server's browser sign-in. Select the saved
connection in the composer to ask the agent to use it.

The connection manager supports HTTPS endpoints and HTTP on loopback for local
servers. It uses the official Python MCP client, including protected-resource and
authorization-server discovery, dynamic client registration when offered, PKCE,
callback state/issuer validation, and refresh tokens. Advanced settings support a
bearer token or the server's pre-registered OAuth client credentials and scopes.
The callback is `/api/integrations/mcp/callback` on the current Workbench origin.
Registration settings must match that exact URL. Servers that do not allow dynamic
registration still require a client registered for Workbench; connecting directly
cannot remove a provider's access restrictions.

### Figma remote requires client approval

The hosted URL `https://mcp.figma.com/mcp` does not need the desktop app, but
[Figma's remote setup guide](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/)
currently limits connections to approved MCP clients. Adding its URL to Workbench
does not grant that approval. Figma's
[staff clarification](https://forum.figma.com/ask-the-community-7/figma-s-mcp-product-57290)
also states that ordinary REST OAuth app credentials do not grant `mcp:connect`.
Use the client request link in the setup guide, or contact the Figma account team
about an internal client on an Organization or Enterprise plan.

Checked 2026-10-20: public discovery advertises OAuth and dynamic registration.
Advertising a registration endpoint does not mean every client can register.
Workbench's earlier error mapped every registration exception to an approval
requirement; that message alone was not evidence of Figma's exact refusal.
The connection screen now explains the provider's documented requirement
separately from the actual registration error. Raw provider errors and credentials
are not displayed.

### Credentials and tool access

Saved connections have independent `mcp_<id>` identifiers. Their host-only state
lives in `runs/.integrations/mcp/`, outside request workspaces. Credentials are never
returned to the browser or agent. Disconnect clears the user grant; Remove deletes
the saved connection. Provider-side consent revocation remains in the provider's
account settings.

The manager shows the live tool catalog. Tools explicitly annotated read-only and
non-destructive are enabled initially. Other tools require the person to enable
them. After a custom selection is saved, newly discovered tools stay disabled.
Discovery returns the exact schema, namespaced name and `dispatch_tool`:

- `integration_call` uses the existing `integration.read` policy and refuses
  non-read-only direct tools.
- `integration_execute` uses a separate `integration.execute` policy for tools the
  user explicitly enabled. It is available to the main agent and general workers;
  specialized read roles retain their existing restrictions.

Both paths pass through Registry, validate live schemas and import bounded results
with provenance into `work/integrations/<connection-id>/`. Enabling a tool is not
authorization for unrelated actions. Remote tool descriptions and outputs are
untrusted source material. A dispatched tool call is not automatically retried
after an ambiguous network failure.

This connection manager supports remote **Streamable HTTP** tools. It does not
launch arbitrary stdio commands, connect legacy HTTP+SSE endpoints, automatically
fetch resource links, or grant server-initiated sampling and elicitation. Those
are separate protocol capabilities, not requirements for ordinary tool servers.

Implementation reference:
[official Python SDK OAuth client](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md).

### Verification, 2026-10-20

246 targeted MCP, existing integration, frontend, HTTP-route and chat/resume tests
pass. The 18 real-wire protocol tests exercise public/bearer/OAuth servers, PKCE,
registration, refresh at a discovered non-root endpoint after reopening, callback
state/issuer/replay checks, connection isolation, permission changes and credential
echo redaction. A live Microsoft Learn connection discovered three tools and
successfully searched through Registry with an imported result and call log.
The visible UI also completed the local OAuth fixture's full redirect/callback
flow and enabled a tool that then executed through Registry. Temporary OAuth
test connections were removed; no production account grant was used in that test.

Tested with MCP SDK 2.3.0 and httpx2 2.13.1. The adapter restores persisted absolute
token expiry in the SDK's initialization hook and wraps the HTTP dialer to pin
validated destination IPs while preserving TLS host checks. These compatibility
points must be rechecked on SDK/transport upgrades.

## Provider presets

Workbench has three independent Figma routes: **Figma MCP (desktop)**, using the
official local MCP server; **Figma direct**, using Figma's REST API from the
Workbench host; and **Figma via Solstice**, using the organization's gateway.
Selecting one never automatically falls back to another. Every agent call goes through
`Registry.call`, the appropriate integration policy row, the role allowlist, and the call log.

## Official Figma desktop MCP

1. Sign in to the Figma desktop app and open the design file. The account needs
   file access and a Dev or Full seat on an eligible plan.
2. With nothing selected, enter Dev Mode (Shift+D). In the right sidebar, enable
   the desktop MCP server. Keep Figma open.
3. In Workbench → Plugins → Manage connections, choose Connect desktop MCP.
   Workbench checks the actual server catalog at `http://127.0.0.1:3845/mcp`.
   No REST OAuth app, Client ID/Secret or Solstice registration is needed.
4. Select Figma MCP (desktop) in Plugins, then send a frame link or explicitly
   ask to use the current desktop selection. A source video is optional.

`integration_tools(integration="figma_mcp")` exposes available known read tools
with a `figma_mcp.` prefix and their live argument schemas. This includes design
context, metadata, screenshots, variable definitions, motion context and Code
Connect reads when the desktop server advertises them. Unknown/write tools are
excluded, even if a remote annotation claims they are read-only. Explicit
non-read-only or destructive hints also exclude a tool. Schemas are checked for
external references and refreshed before dispatch. Workbench identifies itself
as `solstice-video-workbench` and sends no bearer token to the desktop endpoint.

Embedded screenshots and text/code context import under `work/integrations/figma_mcp/`.
Raster imports receive reusable asset refs with external evidence lineage. Exact
HTTP asset links in returned source code or explicit resource-link results may be imported only from
`127.0.0.1:3845/assets/<filename>` or `localhost:3845/assets/<filename>` for PNG,
JPEG, WebP or SVG. No query strings, traversal, other ports/routes or redirects
are fetched. Downloads are bounded at 4 MiB each, 64 files/8 MiB aggregate;
SVG stays inert text. Other resource links remain references, not downloads.
Illustrative URLs inside explanatory prose are not treated as design assets.
The file limit is separate from the eight inline raster images sent to a model;
additional imported assets remain available by workspace path and asset reference.
The original content plus imported assets must fit the 200-block response limit.
Connection preferences persist, but a successful catalog check is not proof
Figma remains open; discovery and dispatch check again. Disconnect only disables
Workbench's connection, not the Figma app's own MCP server.

This implementation connects to the official **desktop** server. The hosted
server offers additional capabilities, including write-to-canvas, but Figma's
[remote setup guide](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/)
currently restricts it to approved clients. Workbench needs its own client
approval before a hosted connection can be offered. The REST app registration
does not grant hosted MCP client approval. See the
[desktop setup guide](https://developers.figma.com/docs/figma-mcp-server/local-server-installation/)
and [tool reference](https://developers.figma.com/docs/figma-mcp-server/tools-and-prompts/).
Tests exercise the actual protocol over simulated HTTP. The user must enable
Figma's desktop server for live reads and keep the intended design open.

Live verification on 2026-10-20 discovered eight supported read tools. Through
Registry, the open social-ad design returned its page metadata, an 820 × 1024
screenshot of MOA / Frame 1, and full design context with all 11 referenced local
assets imported. The large context was saved to `response.json` with a bounded
excerpt returned to the agent. An illustrative URL in Figma's explanatory text
was excluded from downloads. Verification artifacts and call logs are under
`runs/.checks/figma-desktop-20261020/`; no model turn or design edit was run.
All 29 desktop adapter tests pass, including file-count, byte-limit and example-URL
regressions.

## Figma direct: local setup without a Solstice deployment

1. At <https://www.figma.com/developers/apps>, create an OAuth app for your team.
   Save its Client ID and Client Secret. In OAuth credentials, add the exact callback
   `http://127.0.0.1:8765/api/integrations/figma-direct/callback`. Register the callback
   actually shown by your Workbench if you use another local host/port.
2. Enable `file_content:read` and `current_user:read`. The app owner and plan admins
   can test a draft; publish privately for other eligible team/organization members.
3. Open Workbench → Plugins → Manage connections → Figma direct settings. Enter the
   Figma app ID, secret and matching callback. Save Figma settings, then choose
   Connect Figma direct. Complete consent in your regular browser.
4. Choose Check Figma account if needed. Once Connected, select Figma direct from Plugins
   and send a Figma file/frame link in chat.

The host uses one-use state and S256 PKCE, exchanges the code with Figma using the
registered app's credentials, checks `/v1/me`, and stores the grant outside every
agent mount in `runs/.integrations/figma_direct/connection.json` (0600; directory
0700). App secrets never appear in status responses or agent tool schemas. Refresh
is serialized across processes and saved before reads. A blank secret field retains
the saved secret; changing the app ID requires its corresponding secret. Configuration
changes clear the old grant. Disconnect forgets local user tokens and pending sign-in
while retaining app settings; upstream Figma consent is managed separately in Figma.

`integration_tools(integration="figma_direct")` exposes `figma_direct_get_file`
(file key, optional node IDs and depth 1–10, default 2) and
`figma_direct_get_images` (1–8 node IDs, PNG/SVG, scale 0.01–4). Both use the
existing `integration_call` Registry tool. Direct imports emit the existing canvas
event and live in `work/integrations/figma_direct/`, with source URLs and hashes.
PNG is validated before import; SVG is inert text. JSON reads are capped at 2 MiB,
preview downloads at 4 MiB each/8 MiB total. Only fixed Figma API endpoints and
allowlisted HTTPS Figma CDN hosts are contacted. CDN requests carry no Figma
authorization, redirects are rejected, and provider errors do not expose bodies.
Rate limits return an actionable message without automatic retries.

Connected means `/v1/me` verified the account, not that every file is readable.
Manage connections shows the verified account; file access is checked on each read.
The adapter classifies bounded JSON error codes into fixed, safe messages without
echoing arbitrary provider bodies. A 401 or recognized invalid/expired-token 403
can renew the credential and retry once. Generic 403s (including `Request denied`)
do not invalidate a working account or trigger repeated refresh/reconnect attempts.
Explicit insufficient-scope errors point to the app scopes and renewed consent.
The requested scopes are setup requirements, not evidence of the granted scopes.

When a file opens in the browser but REST returns 403, compare the browser account
with the verified account and check the app's configured scopes. Browser access
alone does not establish REST authorization, and `Request denied` does not identify
the underlying Figma rule. Do not claim a specific permission, seat, or app-policy
problem without evidence. Preserve the working grant while investigating.

This route reads designs and renders frames; it does not implement official MCP
design-to-code tools, live desktop selection, editing Figma files or a live Figma
editor. Provider consent and real file access require your own app registration
and have not been verified by the automated transport tests.

## What is implemented

The following sections describe the separate Solstice gateway route.

The host can configure a registered public OAuth client, start an authorization-code
flow with PKCE, validate a one-use callback state, retain the user's grant across
Workbench sessions, rotate refresh tokens under a process-shared lock, and reconnect.
Connection status and tool schemas come from the actual gateway. Merely selecting a
plugin in the composer grants no authority and does not authenticate it.

`integration_status` returns connection/capability status without a network request.
`integration_tools` lists approved read tools and their exact schemas, with query,
integration filter and pagination. `integration_call` refreshes the live catalog,
validates arguments, then invokes one approved read tool. A tool must declare
`readOnlyHint: true`, or be explicitly named by the host in `read_tools`; an explicit
false read-only hint or true destructive hint always prevents use. Figma connect and
disconnect tools are reserved for human UI actions regardless of their annotations.
No remote mutation tools are exposed by this integration.

The transport supports JSON and SSE Streamable HTTP responses, initialization,
protocol/session headers, bounded paginated tool discovery and session termination.
It does not follow redirects, visit advertised sibling servers, accept server sampling
or elicitation, expose roots, or replay uncertain calls. Responses are capped at
16 MiB; catalog discovery is capped at 500 tools and 20 pages. Each HTTP operation
has a 30-second inactivity timeout and a 60-second streaming deadline. Input schemas
cannot retrieve external references. Reconnect is explicit after revoked grants.

Embedded PNG/JPEG/WebP previews are validated and returned as image evidence.
Embedded text/JSON documents and image bytes are saved under
`work/integrations/solstice/<import>/` with source tool, arguments, gateway, retrieval
time and SHA-256 hashes. SVG is preserved as `.svg.txt`, not executed as a document.
Resource links are retained as evidence but never downloaded by Workbench. Imports
emit `integration.imported` in the request ledger and live event feed, with metadata
only. They are external design evidence, not approved copy or accepted video canvases.
Small agent results are unchanged. Larger imports return a bounded excerpt and the
complete `response.json` path instead of putting the full Figma tree into either the
text or structured tool result. Images remain available as visual evidence.

## Required deployment and one-time sign-in

The public Solstice repository at commit
[`092b7b2`](https://github.com/Solstice-Health/solstice-mcp-server/tree/092b7b2d676041f6b5c1e3a0e5ed5ea65d022457)
provides content/review tools, optional memory, and a sibling-server directory. Its
`figma-to-solstice` plugin combines a separate Figma connection with Solstice; it is
not a Figma proxy. The optional upstream Figma extension prepared with this work
requires separate review and deployment. No production gateway or Auth0 settings
are changed by installing Workbench.

The reviewable patch, validation results and deployment prerequisites are in
[`integrations/solstice-figma/`](../integrations/solstice-figma/README.md).

1. Register a **Workbench public/native Auth0 application** with authorization-code
   and refresh-token grants. Do not reuse Cursor's or Codex's registered application.
   Enable API offline access and rotating refresh tokens. Register the exact callback,
   for example `http://127.0.0.1:8765/api/integrations/solstice/callback`.
2. Configure the registered public `client_id`, `redirect_uri` and documented
   `gateway_url` in the Integrations panel. The default gateway is
   `https://api.solsticehealth.co/mcp`. The Auth0 issuer is
   `https://login-solstice.us.auth0.com/`; the exact gateway URL is the token audience.
   Requested scopes are `mcp:connect openid email offline_access`.
3. Choose Connect and sign in as yourself. Workbench exchanges the authorization
   code on the host and verifies access with `tools/list`. A refresh token enables
   persistence; if none is issued, the UI identifies the connection as nonpersistent.
4. For Figma, deploy/configure the Solstice Figma extension, its registered Figma
   OAuth application, and its registered Auth0 web client for browser identity
   verification. Then choose Figma Connect to open the gateway's identity handoff.
   The browser's Solstice subject must match the authenticated MCP caller before
   starting the separate **per-user** Figma grant. A secure browser cookie binds the
   subsequent Figma callback to that browser. Refresh after the callback completes.
   Gateway tool advertisement alone does not mark Figma connected: the gateway must
   report both `configured: true` and `connected: true`.

The same host configuration can be seeded with `VWB_SOLSTICE_MCP_URL`,
`VWB_SOLSTICE_CLIENT_ID`, and `VWB_SOLSTICE_REDIRECT_URI`. These are public settings;
no access token, refresh token, client secret or personal Figma token is accepted
from the agent. Existing `.env` provider credentials are not used to impersonate a
Solstice or Figma user.

OAuth state and credentials are stored in `runs/.integrations/solstice/connection.json`
(file mode 0600, parent directories 0700), outside every request's agent-mounted work
folder. Writes are atomic. Rotation is serialized across processes and persisted
before the new access token is used. Changing gateway/client/callback clears prior
credentials and pending sign-in. Disconnect forgets the local grant; it does not
claim to revoke upstream consent. Protect/back up this host directory accordingly.

## Host API

`from vwb.integrations import IntegrationService, IntegrationError`

`IntegrationService(runs_dir=RUNS_DIR)` offers synchronous `status()`,
`configure(body)`, `begin_auth(redirect_uri)`,
`complete_auth(code, state, redirect_uri)`, `refresh_catalog()`, `disconnect()` and
`begin_figma_auth()`. Network/auth failures raise safe `IntegrationError` messages
without provider response bodies or credentials. Public status contains `items`,
`config` and `setup`; `begin_auth` and `begin_figma_auth` return
`authorization_url` and `expires_at`. Only the validated OAuth callback may accept
the cross-site GET; settings and connect/disconnect routes remain local UI actions.

The gateway extension contract is `solstice_figma_status`,
`solstice_figma_connect`, `solstice_figma_get_file`, and
`solstice_figma_get_images`. Status has structured `{configured, connected, state,
message}`. Connect returns `{authorization_url, expires_in}` for the configured
gateway origin's `/mcp/figma/connect?ticket=...` identity handoff. Direct Figma URLs,
alternate origins and arbitrary redirect parameters are rejected. The gateway
authenticates the browser's Solstice identity before redirecting to Figma; accepting
a transferable provider authorization link alone would allow account-linking CSRF.
Read results carry Figma file/node provenance; render tools return bounded embedded
images/resources, with downloads performed inside the gateway. Figma credentials
remain in the gateway's user-bound vault, never in Workbench.

## Limits and source evidence

The chosen first implementation is a **read adapter inside Solstice**, with per-user
Figma OAuth. It supports the immediate need to bring design structure and rendered
frames into the agent's workspace while keeping the organization gateway in charge.
The alternatives serve different needs:

| Option | Advantage | Deployment tradeoff |
| --- | --- | --- |
| Official remote Figma MCP through Solstice | Broadest vendor features: design context, Code Connect/Make resources and native canvas writes. | New clients require Figma catalog approval. A Solstice bridge would also need user-bound upstream OAuth and a reviewed write policy; forwarding the Solstice bearer token is not a substitute. This is a future extension, not implemented parity. [Figma MCP](https://developers.figma.com/docs/figma-mcp-server/), [MCP resource binding](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) |
| Solstice Figma REST read adapter, chosen | Explicit file/node JSON plus rendered PNG/SVG, bounded imports and provenance; usable with a registered Figma OAuth app and `file_content:read`. | Requires gateway deployment, OAuth app configuration and encrypted per-user credentials. No official MCP design-to-code/write-to-canvas parity. [REST file endpoints](https://developers.figma.com/docs/rest-api/file-endpoints/), [OAuth apps](https://developers.figma.com/docs/rest-api/oauth-apps/) |
| Desktop Figma MCP behind a gateway bridge | Can inspect the active desktop selection and design context. | Requires the signed-in Figma desktop app, enabled local server and a bridge from Solstice to that machine. The documented endpoint is loopback `127.0.0.1:3845/mcp`; it does not provide a remotely hosted gateway deployment by itself. [Desktop setup](https://developers.figma.com/docs/figma-mcp-server/local-server-installation/) |
| Live Figma embed in the viewer | Preserves interactive file/prototype viewing, frame/page targeting and familiar Figma rendering. | A complementary viewer, not agent data authorization. Browser Figma authentication and Storage Access API behavior remain separate; prototype messaging requires registered embed origins. It cannot replace the gateway's user OAuth grant or deliver structured tool evidence. [Embed Kit 2.0](https://developers.figma.com/docs/embeds/embed-kit-2.0/) |

The bridge requirements in the table are architectural conclusions from the gateway
constraint and OAuth resource binding, not claims that Figma supplies a turnkey
Solstice proxy. A live embed can be added independently for viewing; it must not be
reported as an authenticated agent integration.

This is a gateway REST adapter for Figma reads, not full official Figma MCP parity.
Figma's official MCP documentation currently restricts connections to clients in its
MCP catalog; a new custom client must use its registration/waitlist process. The
separate Figma REST API supports registered OAuth apps. Neither condition authorizes
silently bypassing the organization's gateway or borrowing another client's identity.

Primary sources checked on 2026-10-14:

- [Solstice client setup and exact audience/callback requirements](https://github.com/Solstice-Health/solstice-mcp-server/blob/092b7b2d676041f6b5c1e3a0e5ed5ea65d022457/docs/MCP_CLIENT_SETUP.md)
- [Figma MCP client availability](https://developers.figma.com/docs/figma-mcp-server/)
- [Figma REST authentication](https://developers.figma.com/docs/rest-api/authentication/)
- [Figma OAuth application registration](https://developers.figma.com/docs/rest-api/oauth-apps/)
- [MCP authorization and resource binding](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Auth0 public-client PKCE flow](https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow-with-pkce/call-your-api-using-the-authorization-code-flow-with-pkce)
- [Auth0 refresh-token rotation](https://auth0.com/docs/secure/tokens/refresh-tokens/refresh-token-rotation)

Tests use bounded fake HTTP/MCP transports and exercise the real Registry and artifact
store; they do not claim that production registration, user sign-in, or the undeployed
Figma extension has been verified live.
