# Protected public access

Public access is opt-in. With no public options or `VWB_UI_PUBLIC_URL`, `uv run vwb ui`
keeps its existing localhost behavior at `http://127.0.0.1:8765`. Public mode still
binds to loopback; ngrok or your reverse proxy supplies the HTTPS endpoint.

The private launch link gives a **trusted group full access to this host's
Workbench**: shared conversations, files, settings and connected tools. There are
no per-user accounts, permissions or workspace isolation at this gate. Share it
only with people trusted to operate the same Workbench and its integrations.

## Start with ngrok

Install `ngrok` on the Workbench host and make it available on `PATH`. Configure
its authtoken on that host, reusing the existing ngrok configuration if present.
Workbench does not install ngrok or manage its credentials. Never copy the
authtoken into repository files, `.env.example`, documentation or commits.

If port 8765 is busy, coordinate with the current server's operator before
restarting it in public mode. Do not launch a second UI against the same `runs/`,
even on another port. Workbench binds its own port before starting ngrok and
refuses an occupied port; it cannot attach a tunnel to an already running UI.

Choose one launch command:

```bash
uv run vwb ui --ngrok --no-open
# Or reuse a domain reserved/configured in your ngrok account:
uv run vwb ui --ngrok --public-url https://workbench.example --no-open
```

`workbench.example` is a placeholder; replace it with your public hostname.
With neither `--public-url` nor `VWB_UI_PUBLIC_URL`, Workbench discovers the HTTPS
origin assigned by ngrok.
`--no-open` prints the links without opening a browser. The managed tunnel disables
ngrok traffic inspection. Press **Ctrl-C** in the launching terminal to stop the
UI and its managed ngrok process and clean up temporary tunnel logs. Queued
messages are kept.

`VWB_UI_PUBLIC_URL` is an optional, nonsecret alternative to `--public-url`;
the flag takes precedence. Export it in the launching shell or service environment.
The provider-key `.env` loader does not export UI settings. The value must be an
HTTPS origin with a public hostname and optional port, with no trailing slash,
path, credentials, query or fragment. For default local mode, leave this setting
unset and omit both public flags.

## Open and share the private link

Use the terminal's **Private launch link**, shaped like
`https://workbench.example/#access=<grant>`. The grant is a secret even though the
origin is public. Keep the complete link out of commits, public logs and screenshots.
The browser removes the fragment from the address bar and exchanges it for an
HttpOnly session cookie (Secure on the public HTTPS origin). Fragments do not
enter proxy request URLs. Without a valid session, the bare origin shows an
access-code prompt.

The free ngrok tier may show its one-time **Visit Site** page first. Continue to
the hostname you expect; the private launch link then opens Workbench.

Workspace and artifact previews use narrowly scoped capability URLs so isolated
HTML previews can load their assets. Those links are also private and expire when
the server restarts; they do not grant access to the Workbench API.

Public mode also protects local access; use the printed **Local launch link**
on the host. Restarting the UI rotates the launch grant and session token, so old
links and cookies expire. Get the new link from the host. This does not revoke
the integration OAuth grants stored on that host.

## Choose who owns the URL

- **One shared URL:** one host/Workbench owns the endpoint at a time. Collaborators
  share its state. Stop the old tunnel before moving that URL to another host;
  files and integration grants do not move with the URL.
- **Separate per-person URLs:** each person runs a separate Workbench with its own
  runs directory, integration state and tunnel URL. Register each exact callback
  needed by those installations.

Do not enable ngrok endpoint pooling across independent Workbench hosts. Their
access cookies, OAuth state, files and queues are independent; requests must reach
the host that owns them. Workbench does not enable pooling.

## Register the Solstice callback

On the **Workbench public/native Auth0 client**, register the exact public HTTPS
callback printed at startup, for example:

```text
https://workbench.example/api/integrations/solstice/callback
```

Use that same callback and the Workbench client's Solstice Client ID in
**Plugins → Manage connections**, then Connect and complete Solstice sign-in
from the browser that opened the public launch link. The tunnel and access grant
do not replace client registration or provider sign-in. Do not repurpose an
unrelated client's callback or borrow its Client ID. A changed public URL needs
its new exact callback registered and configured before reconnecting.
See [integration setup](INTEGRATIONS.md#required-deployment-and-one-time-sign-in)
for Auth0 grants/scopes and the other providers' callback paths.

## Use an existing reverse proxy

```bash
uv run vwb ui --public-url https://workbench.example --no-open
```

This enables the same access gate without starting ngrok. Configure your proxy to
terminate HTTPS at that exact origin and forward to `http://127.0.0.1:8765`,
preserving the public **Host** header. Do not rewrite Host to localhost; callback
URLs and origin checks use Host and the configured origin, not `Forwarded` or
`X-Forwarded-*`. Workbench accepts only its configured public host and local
addresses. Ctrl-C stops the UI; an externally managed proxy has its own lifecycle.
