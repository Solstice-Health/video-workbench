"""Explicit public-origin access for a single, trusted-group Workbench.

The launch grant is a URL fragment, so it never enters proxy request URLs.
Cookies authorize this process only; this is not a multi-user permissions system.
"""
from __future__ import annotations

import hmac
import secrets
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from vwb.integrations.callbacks import public_origin


class PublicAccess:
    def __init__(self, origin: str):
        self.origin = public_origin(origin)
        self.launch_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)

    @property
    def host(self):
        return urlsplit(self.origin).netloc

    @property
    def launch_url(self):
        return f'{self.origin}/#access={self.launch_token}'

    def cookie_name(self, handler):
        return '__Host-vwb_access' if handler._request_origin() == self.origin else f'vwb_local_access_{handler.server.server_port}'

    def authorized(self, handler):
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get('Cookie', ''))
            value = cookie.get(self.cookie_name(handler))
            return bool(value and hmac.compare_digest(value.value.encode(), self.session_token.encode()))
        except (CookieError, ValueError, TypeError):
            return False

    def gate(self, handler, url, parts):
        """Return true if handled; all other requests require a session.

        Isolated previews use their existing per-artifact capability URLs. Their
        opaque-origin subresources cannot rely on the application's cookies.
        """
        handler._check_host()
        if parts == ['__workbench', 'session'] and handler.command == 'POST':
            handler._check_origin()
            body = handler._body()
            token = body.get('token')
            if set(body) != {'token'} or not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.launch_token.encode()):
                handler._json({'error': 'This access link is invalid or expired. Ask the host for its current launch link.'}, 401)
                return True
            secure = '; Secure' if handler._request_origin() == self.origin else ''
            handler.send_response(204)
            handler.send_header('Set-Cookie', f'{self.cookie_name(handler)}={self.session_token}; HttpOnly; SameSite=Lax; Path=/{secure}')
            handler.send_header('Set-Cookie', f'vwb_qc_{handler.server.server_port}={handler.app.qc_token}; HttpOnly; SameSite=Strict; Path=/api/requests/{secure}')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return True
        if self.authorized(handler):
            return False
        if handler.command in {'GET', 'HEAD'} and parts[0] in {'preview', 'artifact-preview'}:
            return False  # The normal route must verify its scoped capability.
        if handler.command in {'GET', 'HEAD'} and parts == ['']:
            # The login page contains no secrets. Links from email/chat must
            # permit cross-site top-level navigation; the exchange stays
            # same-origin and requires the unguessable fragment grant.
            nonce = secrets.token_urlsafe(24)
            payload = _login_page(nonce).encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/html; charset=utf-8')
            handler.send_header('Content-Length', str(len(payload)))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Referrer-Policy', 'no-referrer')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            handler.end_headers()
            if handler.command != 'HEAD':
                handler.wfile.write(payload)
            return True
        handler.close_connection = True  # A rejected upload body may still be unread.
        handler._json({'error': 'Workbench access required. Open the current launch link supplied by its host.'}, 401)
        return True


def _login_page(nonce):
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open Workbench</title><style nonce="{nonce}">
body{{margin:0;background:#f7f5f1;color:#252421;font:16px/1.5 system-ui;display:grid;min-height:100vh;place-items:center}}
main{{max-width:430px;padding:32px}}h1{{font-size:28px;font-weight:500}}input,button{{font:inherit;padding:12px;border-radius:8px;border:1px solid #bbb;box-sizing:border-box;width:100%}}
button{{background:#252421;color:#fff;margin-top:12px;cursor:pointer}}label{{display:block;margin-bottom:8px}}p{{color:#595650}}#status{{min-height:24px}}
</style></head><body><main><h1>Open the shared Workbench</h1>
<p>Use the launch link or access code supplied by the person running this Workbench. Access includes this host's shared conversations, files and connected tools.</p>
<form id="login"><label for="code">Access code</label><input id="code" type="password" autocomplete="off" required>
<button>Open Workbench</button></form><p id="status" role="status"></p></main>
<script nonce="{nonce}">
const input=document.querySelector('#code'),status=document.querySelector('#status'),form=document.querySelector('#login');
const grant=new URLSearchParams(location.hash.slice(1)).get('access');
history.replaceState(null,'',location.pathname);
async function enter(token){{
 status.textContent='Opening…'; form.querySelector('button').disabled=true;
 try{{const response=await fetch('/__workbench/session',{{method:'POST',headers:{{'Content-Type':'application/json'}},credentials:'same-origin',body:JSON.stringify({{token}})}});
 if(!response.ok)throw new Error('This access link is invalid or expired. Ask the host for its current link.');
 location.replace('/');}}catch(error){{status.textContent=error.message;form.querySelector('button').disabled=false;}}
}}
form.addEventListener('submit',event=>{{event.preventDefault();enter(input.value);input.value='';}});
if(grant)enter(grant);
</script></body></html>'''
