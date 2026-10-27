"""Optional central SSO entry point; local users and permissions remain authoritative."""
from __future__ import annotations

import json
import os
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request as HTTPRequest, build_opener
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import db

APP_CODE = "BANK_RECO"


class SSOFailure(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the client secret to another endpoint.
        return None


def validate_ticket(ticket: str) -> str:
    url = os.environ.get("SSO_VALIDATE_URL", "")
    secret = os.environ.get("SSO_CLIENT_SECRET", "")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SSOFailure("configuration") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or len(secret) < 32):
        raise SSOFailure("configuration")
    body = urlencode({"ticket": ticket, "clientSecret": secret, "appCode": APP_CODE}).encode("utf-8")
    req = HTTPRequest(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "BankReconciliation-SSO/1.0",
    })
    try:
        with build_opener(NoRedirect()).open(req, timeout=10) as response:
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                raise SSOFailure("response")
        result = json.loads(raw)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        # Do not expose transport errors, tickets, secrets or upstream response bodies.
        raise SSOFailure("validation") from exc
    if not isinstance(result, dict) or result.get("Success") is not True:
        raise SSOFailure("rejected")
    username = result.get("LegacyUserId")
    if not isinstance(username, str) or not username.strip() or len(username) > 200:
        raise SSOFailure("identity")
    return username


def make_router(start_session, root_path: str) -> APIRouter:
    router = APIRouter()

    @router.get("/sso/receive", response_class=HTMLResponse)
    def receive(request: Request):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if os.environ.get("SSO_ENABLED", "0") != "1":
            return HTMLResponse("SSO is not enabled.", status_code=404, headers=headers)
        tickets = request.query_params.getlist("ticket")
        try:
            if len(tickets) != 1 or len(tickets[0]) != 36:
                raise ValueError("ticket")
            ticket = str(UUID(tickets[0]))
        except ValueError:
            return HTMLResponse("Invalid SSO request. Launch again from the SSO portal.",
                                status_code=400, headers=headers)
        try:
            username = validate_ticket(ticket)
        except SSOFailure:
            return HTMLResponse("SSO login could not be validated. Launch again from the SSO portal "
                                "or contact your administrator.", status_code=401, headers=headers)

        c = db.connect()
        try:
            with db.tx(c):
                row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                if not row or not row["active"]:
                    return HTMLResponse("No active Bank Reconciliation account is mapped to this SSO login. "
                                        "Contact your administrator.", status_code=403, headers=headers)
                # The existing helper rotates the nonce and creates the same session as password login.
                # No account creation, password update, role assignment or permission bypass.
                start_session(request, c, row["username"])
                c.execute("UPDATE users SET last_login=? WHERE id=?", (db.now(), row["id"]))
                db.audit(c, row["username"], "sso_login", "Central SSO ticket validated")
        finally:
            c.close()
        destination = "/me" if row["must_change_pw"] else "/"
        # A same-site document navigation lets the existing SameSite=Strict cookie work
        # after a launch from a different site. Keep the app's cookie policy unchanged.
        target = escape(root_path + destination, quote=True)
        return HTMLResponse(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="referrer" content="no-referrer">'
            '<meta http-equiv="refresh" content="0;url=' + target + '">'
            '<title>Signing in</title></head><body>'
            '<p>Signed in. <a href="' + target + '">Continue to Bank Reconciliation</a>.</p>'
            '</body></html>', headers=headers)

    return router
