# Solstice gateway Figma extension

`upstream.patch` is an undeployed, disabled-by-default extension for the separate
Solstice MCP gateway repository at commit
`092b7b2d676041f6b5c1e3a0e5ed5ea65d022457`. It is not installed into Video Workbench
and does not change the upstream deployment. The Workbench client can use its
four tools after an operator applies, configures, validates and deploys it.

The extension supplies per-user Figma REST file/node reads and embedded PNG/SVG
exports through Solstice. It is **not** a custom client for the official Figma MCP
server. No Solstice bearer is forwarded to Figma. Read-only file permissions are
enforced by each user's separate Figma OAuth grant.

Apply in a clean checkout of the Solstice MCP repository:

```sh
git switch --detach 092b7b2d676041f6b5c1e3a0e5ed5ea65d022457
git apply --check /absolute/path/to/integrations/solstice-figma/upstream.patch
git apply /absolute/path/to/integrations/solstice-figma/upstream.patch
```

Read the resulting `docs/FIGMA_REST.md` before rollout. It contains the API
contract, secret settings, explicit migration, callback/logging requirements,
limits and test commands. The patch includes that document, SQL migration,
source, direct dependency declaration and tests. No environment, credentials,
cache or user grant is included.

Deployment requires two application registrations:

- A Figma REST OAuth app, normally published privately for the intended
  organization, with `file_content:read current_user:read` and exact callback
  `https://api.solsticehealth.co/mcp/figma/callback`.
- A separate Auth0 regular web application in the existing Solstice Auth0 tenant,
  using RS256 ID tokens and exact callback
  `https://api.solsticehealth.co/mcp/figma/identity/callback`. It verifies that the
  browser's signed-in subject matches the MCP caller before Figma consent.

Use the configured staging origin instead of the production example when
staging. These registrations are separate from the Workbench's existing Auth0
public-client/loopback registration. The browser flow uses one-use ticket/state,
PKCE, OIDC nonce/signature checks and a rotated Secure HttpOnly SameSite=Lax cookie;
a forwarded authorization link alone cannot bind another person's Figma account.

Operators must provision PostgreSQL and encryption keys, explicitly apply
`migrations/20261014_figma.sql`, register both apps, configure `FIGMA_*` settings,
route all `/mcp/figma/*` endpoints, redact OAuth callback query logs, and complete
the documented staging checks before enabling the integration. The gateway never
creates production tables automatically. Unsetting `FIGMA_ENABLED` removes the
Figma tool registrations without deleting stored grants.

Verification: 36 focused tests and all 613 upstream tests passed; targeted Ruff,
new-module mypy and clean patch application passed. The applied files were compared
byte-for-byte with the reviewed clone. See `validation.json` for the patch hash
and exact limits of this evidence. The two upstream warnings concern Starlette's
HTTP test client and an existing short HMAC fixture key.

Live PostgreSQL behavior, real Auth0/Figma consent, hosted callback configuration
and provider image downloads remain untested. No deployment or paid provider
session was performed. The staging checks in the upstream document are required
before claiming a live Figma connection.
