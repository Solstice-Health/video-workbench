# Solstice and Figma integrations

Workbench uses the organization's Solstice MCP gateway. It does not contact Figma MCP,
Figma REST, or sibling MCP servers directly. Every agent call goes through
`Registry.call`, the `integration.read` policy row, the role allowlist, and the call log.

## What is implemented

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
