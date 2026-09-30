"""Web application."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import accounts_import, auth, db, engine, report
from .parsers import inr

BASE = Path(__file__).resolve().parent
STAGING = db.DATA_DIR / "staging"
MAX_UPLOAD = 25 * 1024 * 1024
ALLOWED = {".xls", ".xlsx", ".csv"}
KEEP_UPLOADS = os.environ.get("KEEP_UPLOADS", "0") == "1"
# Serve under a sub-path, e.g. ROOT_PATH=/bankreco  ->  https://host/bankreco/
ROOT_PATH = "/" + os.environ.get("ROOT_PATH", "").strip("/") if os.environ.get("ROOT_PATH", "").strip("/") else ""

db.init()
_c = db.connect()
auth.ensure_env_user(_c)
_c.close()

app = FastAPI(title="Bank Reconciliation", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=auth.secret_key(), max_age=8 * 3600, same_site="strict",
                   https_only=os.environ.get("HTTPS_ONLY", "0") == "1", session_cookie="bankreco",
                   path=ROOT_PATH or "/")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _dmy(v):
    if not v:
        return ""
    if isinstance(v, str):
        try:
            v = date.fromisoformat(v[:10])
        except ValueError:
            return v
    return v.strftime("%d-%m-%Y")


templates.env.filters["inr"] = lambda v: "" if v is None or v == "" else inr(float(v))
templates.env.filters["dmy"] = _dmy
templates.env.globals["now"] = datetime.now
templates.env.globals["base"] = ROOT_PATH


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------- helpers

def conn():
    return db.connect()


def user(request: Request) -> str | None:
    return request.session.get("user")


def safe_back(url: str) -> str:
    """Only same-site relative paths (blocks //evil.com and /\\evil.com)."""
    if not url.startswith("/") or url.startswith("//") or url.startswith("/\\") or "\\" in url:
        return "/"
    return url


def csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, token: str) -> bool:
    return bool(token) and secrets.compare_digest(token, request.session.get("csrf", ""))


def render(request: Request, name: str, **ctx):
    ctx.update(request=request, user=user(request), csrf=csrf(request),
               flash=request.session.pop("flash", None))
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, flash: tuple[str, str] | None = None, request: Request | None = None):
    if flash and request is not None:
        request.session["flash"] = {"kind": flash[0], "msg": flash[1]}
    if url.startswith("/"):
        url = ROOT_PATH + url
    return RedirectResponse(url, status_code=303)


def guard(request: Request):
    """Returns a redirect if not logged in, else None."""
    c = conn()
    try:
        if auth.user_count(c) == 0:
            return RedirectResponse(ROOT_PATH + "/setup", status_code=303)
    finally:
        c.close()
    if not user(request):
        return RedirectResponse(ROOT_PATH + "/login", status_code=303)
    c = conn()
    try:
        row = c.execute("SELECT session_nonce FROM users WHERE username=?", (user(request),)).fetchone()
    finally:
        c.close()
    if not row or not row["session_nonce"] or not secrets.compare_digest(row["session_nonce"],
                                                                          request.session.get("nonce", "")):
        request.session.clear()
        return RedirectResponse(ROOT_PATH + "/login", status_code=303)
    return None


def _start_session(request: Request, c, username: str) -> None:
    nonce = secrets.token_urlsafe(24)
    c.execute("UPDATE users SET session_nonce=? WHERE username=?", (nonce, username))
    request.session.clear()
    request.session["user"] = username
    request.session["nonce"] = nonce


def accounts_overview(c):
    accs = [dict(a) for a in c.execute("SELECT * FROM accounts ORDER BY kind DESC, active DESC, nickname")]
    for a in accs:
        last = engine.latest_run(c, a["id"])
        a["last"] = dict(last) if last else None
        if last:
            a["brs"] = json.loads(last["brs_json"] or "{}")
            a["summary"] = json.loads(last["summary_json"] or "{}")
            opn = engine.open_items_as_of(c, a["id"], last["id"])
            a["open_count"] = len(opn)
            a["open_bank"] = round(sum(i["amount"] for i in opn if i["side"] == "bank"), 2)
            a["open_book"] = round(sum(i["amount"] for i in opn if i["side"] == "book"), 2)
            a["oldest_open"] = min((i["date"] for i in opn), default=None)
    return accs


# ---------------------------------------------------------------------------- auth routes

@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    c = conn()
    try:
        if auth.user_count(c):
            return RedirectResponse(ROOT_PATH + "/login", status_code=303)
    finally:
        c.close()
    return render(request, "setup.html")


@app.post("/setup")
def setup(request: Request, username: str = Form(...), password: str = Form(...), password2: str = Form(...),
          csrf_token: str = Form("")):
    if not check_csrf(request, csrf_token):
        return redirect("/setup", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        if auth.user_count(c):
            return RedirectResponse(ROOT_PATH + "/login", status_code=303)
        if len(password) < 10:
            return redirect("/setup", ("error", "Use at least 10 characters for the password."), request)
        if password != password2:
            return redirect("/setup", ("error", "Passwords do not match."), request)
        c.execute("INSERT INTO users(username, pw_hash, created_at) VALUES (?,?,?)",
                  (username.strip(), auth.hash_pw(password), db.now()))
        db.audit(c, username.strip(), "setup", "Login created")
        _start_session(request, c, username.strip())
    finally:
        c.close()
    return redirect("/accounts", ("ok", "Login created. Add your bank accounts to get started."), request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    c = conn()
    try:
        if auth.user_count(c) == 0:
            return RedirectResponse(ROOT_PATH + "/setup", status_code=303)
    finally:
        c.close()
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    ip = request.client.host if request.client else "?"
    wait = auth.locked(ip)
    if wait:
        return redirect("/login", ("error", f"Too many attempts. Try again in {wait // 60 + 1} min."), request)
    if not check_csrf(request, csrf_token):
        return redirect("/login", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        if not row or not auth.verify_pw(password, row["pw_hash"]):
            auth.record_fail(ip)
            db.audit(c, username.strip() or "?", "login_failed", f"from {ip}")
            return redirect("/login", ("error", "Wrong username or password."), request)
        auth.clear_fails(ip)
        db.audit(c, row["username"], "login", f"from {ip}")
        _start_session(request, c, row["username"])
    finally:
        c.close()
    return RedirectResponse(ROOT_PATH + "/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    if user(request) and check_csrf(request, csrf_token):
        c = conn()
        try:  # rotating the nonce kills this cookie even if someone kept a copy
            c.execute("UPDATE users SET session_nonce=? WHERE username=?", (secrets.token_urlsafe(24), user(request)))
            db.audit(c, user(request), "logout")
        finally:
            c.close()
    request.session.clear()
    return RedirectResponse(ROOT_PATH + "/login", status_code=303)


@app.post("/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), new2: str = Form(...),
                    csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/accounts", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (user(request),)).fetchone()
        if not auth.verify_pw(current, row["pw_hash"]):
            return redirect("/accounts", ("error", "Current password is wrong."), request)
        if len(new) < 10 or new != new2:
            return redirect("/accounts", ("error", "New password must be 10+ characters and match."), request)
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_pw(new), row["id"]))
        db.audit(c, user(request), "password_changed")
        _start_session(request, c, row["username"])   # signs out every other session
    finally:
        c.close()
    return redirect("/accounts", ("ok", "Password changed."), request)


# ---------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        accs = accounts_overview(c)
        recent = [dict(r) for r in c.execute(
            "SELECT r.*, a.nickname, a.acc_no FROM runs r JOIN accounts a ON a.id=r.account_id "
            "ORDER BY r.id DESC LIMIT 8")]
        for r in recent:
            r["brs"] = json.loads(r["brs_json"] or "{}")
            r["summary"] = json.loads(r["summary_json"] or "{}")
    finally:
        c.close()
    return render(request, "dashboard.html", accounts=accs, recent=recent)


# ---------------------------------------------------------------------------- accounts

@app.get("/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        accs = [dict(a) for a in c.execute("SELECT * FROM accounts ORDER BY kind DESC, nickname")]
        for a in accs:
            a["runs"] = c.execute("SELECT COUNT(*) FROM runs WHERE account_id=?", (a["id"],)).fetchone()[0]
            lr = engine.latest_run(c, a["id"])
            a["last_run_id"] = lr["id"] if lr else None
            a["last_period"] = lr["period_to"] if lr else None
    finally:
        c.close()
    edit = request.query_params.get("edit")
    return render(request, "accounts.html", accounts=accs, import_report=request.session.pop("import_report", None), edit=int(edit) if edit and edit.isdigit() else None)


@app.get("/accounts/{account_id}/view")
def account_view(request: Request, account_id: int):
    """Open the latest reconciliation of an account (or its history if none yet)."""
    if (g := guard(request)):
        return g
    c = conn()
    try:
        lr = engine.latest_run(c, account_id)
    finally:
        c.close()
    if lr:
        return redirect(f"/runs/{lr['id']}")
    return redirect(f"/reconcile?account={account_id}", ("error", "This account has not been reconciled yet."), request)


@app.get("/accounts/template")
def accounts_template(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        rows = [[a["bank"], a["acc_no"], a["nickname"], "Main" if a["kind"] == "main" else "Collection",
                 a["branch"], a["notes"], "Yes" if a["active"] else "No"]
                for a in c.execute("SELECT * FROM accounts ORDER BY kind DESC, branch, acc_no")]
    finally:
        c.close()
    data = accounts_import.template_bytes(rows or None)
    name = "bank_accounts.xlsx" if rows else "bank_accounts_template.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/accounts/import")
def accounts_bulk_import(request: Request, file: UploadFile = File(...), default_kind: str = Form("collection"),
                         main_last4: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/accounts", ("error", "Session expired, try again."), request)
    token = secrets.token_hex(8)
    folder = STAGING / token
    folder.mkdir(parents=True, exist_ok=True)
    try:
        try:
            path, _ = _stage(file, folder)
        except ValueError as e:
            return redirect("/accounts", ("error", str(e)), request)
        mains = {re.sub(r"\D", "", x)[-4:] for x in main_last4.split(",") if re.sub(r"\D", "", x)}
        try:
            rows, problems = accounts_import.parse_accounts(
                str(path), "main" if default_kind == "main" else "collection", main_last4=mains)
        except Exception as e:
            return redirect("/accounts", ("error", f"Could not read the file: {e}"), request)
        if not rows:
            request.session["import_report"] = problems[:30]
            return redirect("/accounts", ("error", "No accounts found in that file."), request)
        c = conn()
        try:
            res = accounts_import.apply_import(c, rows, user(request))
        finally:
            c.close()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    request.session["import_report"] = (problems + res["notes"])[:30]
    return redirect("/accounts", ("ok", f"Import done: {res['added']} added, {res['updated']} updated, "
                                        f"{res['unchanged']} unchanged."), request)


@app.post("/accounts")
def save_account(request: Request, bank: str = Form(...), acc_no: str = Form(...), nickname: str = Form(...),
                 kind: str = Form(...), branch: str = Form(""), notes: str = Form(""), active: str = Form("1"),
                 account_id: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/accounts", ("error", "Session expired, try again."), request)
    acc_no = acc_no.strip().replace(" ", "")
    if kind not in ("main", "collection") or len(acc_no) < 4 or not acc_no.isdigit():
        return redirect("/accounts", ("error", "Account number must be digits (at least the last 4)."), request)
    c = conn()
    try:
        if account_id:
            used = c.execute("SELECT COUNT(*) FROM runs WHERE account_id=?", (account_id,)).fetchone()[0]
            old = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if used and (old["acc_no"] != acc_no or old["kind"] != kind):
                return redirect("/accounts", ("error", "Account number and type cannot change once reconciled."),
                                request)
            c.execute("UPDATE accounts SET bank=?, acc_no=?, nickname=?, kind=?, branch=?, notes=?, active=? WHERE id=?",
                      (bank.strip(), acc_no, nickname.strip(), kind, branch.strip(), notes.strip(),
                       1 if active == "1" else 0, account_id))
            db.audit(c, user(request), "account_updated", f"#{account_id} {nickname}")
        else:
            try:
                c.execute("INSERT INTO accounts(bank, acc_no, nickname, kind, branch, notes, created_at) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (bank.strip(), acc_no, nickname.strip(), kind, branch.strip(), notes.strip(), db.now()))
            except Exception:
                return redirect("/accounts", ("error", "That account already exists."), request)
            db.audit(c, user(request), "account_added", f"{bank} {acc_no} {nickname} ({kind})")
    finally:
        c.close()
    return redirect("/accounts", ("ok", "Account saved."), request)


# ---------------------------------------------------------------------------- reconcile

def _stage(upload: UploadFile, folder: Path) -> tuple[Path, str]:
    name = Path(upload.filename or "file").name
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"{name}: only .xls, .xlsx or .csv files are accepted.")
    dest = folder / f"{secrets.token_hex(4)}{ext}"
    size = 0
    with open(dest, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise ValueError(f"{name} is larger than 25 MB.")
            out.write(chunk)
    return dest, name


def _cleanup_staging():
    if not STAGING.exists():
        return
    for d in STAGING.iterdir():
        if d.is_dir() and time.time() - d.stat().st_mtime > 3600:
            shutil.rmtree(d, ignore_errors=True)


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_form(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        accs = accounts_overview(c)
    finally:
        c.close()
    pre = request.query_params.get("account")
    return render(request, "reconcile.html", accounts=[a for a in accs if a["active"]],
                  preselect=int(pre) if pre and pre.isdigit() else None)


def _finish_upload(request, token, meta, acknowledged=False, reason=""):
    folder = STAGING / token
    c = conn()
    try:
        try:
            rid = engine.reconcile(c, meta["account_id"], meta["bank_path"], meta["bank_name"], meta["book_path"],
                                   meta["book_name"], user(request), acknowledged, reason)
        except engine.Blocked as e:
            acc = c.execute("SELECT * FROM accounts WHERE id=?", (meta["account_id"],)).fetchone()
            return render(request, "blocked.html", checks=e.checks, ackable=e.ackable, token=token,
                          account=dict(acc), meta=meta)
        except Exception as e:  # unreadable file etc.
            shutil.rmtree(folder, ignore_errors=True)
            return redirect("/reconcile", ("error", f"Could not process the files: {e}"), request)
    finally:
        c.close()
    if KEEP_UPLOADS:
        keep = db.DATA_DIR / "uploads" / str(rid)
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy(meta["bank_path"], keep / meta["bank_name"])
        shutil.copy(meta["book_path"], keep / meta["book_name"])
    shutil.rmtree(folder, ignore_errors=True)
    return redirect(f"/runs/{rid}", ("ok", "Reconciliation complete."), request)


@app.post("/reconcile")
def reconcile(request: Request, account_id: int = Form(...), bank_file: UploadFile = File(...),
              book_file: UploadFile = File(...), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/reconcile", ("error", "Session expired, try again."), request)
    _cleanup_staging()
    token = secrets.token_hex(12)
    folder = STAGING / token
    folder.mkdir(parents=True, exist_ok=True)
    try:
        bpath, bname = _stage(bank_file, folder)
        kpath, kname = _stage(book_file, folder)
    except ValueError as e:
        shutil.rmtree(folder, ignore_errors=True)
        return redirect("/reconcile", ("error", str(e)), request)
    meta = {"account_id": account_id, "bank_path": str(bpath), "bank_name": bname,
            "book_path": str(kpath), "book_name": kname, "user": user(request)}
    (folder / "meta.json").write_text(json.dumps(meta))
    return _finish_upload(request, token, meta)


@app.post("/reconcile/ack")
def reconcile_ack(request: Request, token: str = Form(...), reason: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if not check_csrf(request, csrf_token) or not token.isalnum():
        return redirect("/reconcile", ("error", "Session expired, upload again."), request)
    mp = STAGING / token / "meta.json"
    if not mp.exists():
        return redirect("/reconcile", ("error", "Upload expired (1 hour). Please upload again."), request)
    meta = json.loads(mp.read_text())
    if meta.get("user") != user(request):
        return redirect("/reconcile", ("error", "Upload belongs to another session."), request)
    return _finish_upload(request, token, meta, True, reason)


@app.post("/reconcile/cancel")
def reconcile_cancel(request: Request, token: str = Form(...), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if check_csrf(request, csrf_token) and token.isalnum():
        shutil.rmtree(STAGING / token, ignore_errors=True)
    return redirect("/reconcile")


# ---------------------------------------------------------------------------- run view

def _group_matches(items, matches):
    by = {}
    for it in items:
        if it.get("match_id") in matches:
            by.setdefault(it["match_id"], {"bank": [], "book": []})[it["side"]].append(it)
    out = {"out": [], "in": []}
    for mid, g in by.items():
        m = matches[mid]
        direction = (g["bank"] or g["book"])[0]["dir"]
        out[direction].append({"m": m, "bank": g["bank"], "book": g["book"],
                               "amount": round(sum(i["amount"] for i in g["bank"]), 2)})
    order = {"review": 0, "manual": 1, "strong": 2}
    for d in out:
        out[d].sort(key=lambda x: (order.get(x["m"]["confidence"], 3), x["m"]["id"]))
    return out


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: int):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return redirect("/", ("error", "Run not found."), request)
        acc = dict(c.execute("SELECT * FROM accounts WHERE id=?", (run["account_id"],)).fetchone())
        items = engine.run_items(c, run_id)
        matches = {m["id"]: m for m in engine.run_matches(c, run_id)}
        open_ = engine.open_items_as_of(c, run["account_id"], run_id)
        latest = engine.latest_run(c, run["account_id"])
    finally:
        c.close()
    run = dict(run)
    flags = json.loads(run["flags_json"] or "[]")
    for f in flags:
        f["keyset"] = ",".join(f["keys"])
    return render(request, "run.html", run=run, account=acc,
                  checks=json.loads(run["checks_json"] or "[]"), flags=flags,
                  brs=json.loads(run["brs_json"] or "{}"), s=json.loads(run["summary_json"] or "{}"),
                  groups=_group_matches(items, matches), open_items=open_,
                  is_latest=latest and latest["id"] == run_id,
                  as_of=date.fromisoformat(run["period_to"]))


@app.get("/runs/{run_id}/excel")
def run_excel(request: Request, run_id: int):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        data, fname = report.build_report(c, run_id)
        db.audit(c, user(request), "download", f"Run #{run_id} Excel")
    finally:
        c.close()
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/runs/{run_id}/undo")
def run_undo(request: Request, run_id: int, confirm: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    if not check_csrf(request, csrf_token) or confirm.strip().upper() != "UNDO":
        return redirect(f"/runs/{run_id}", ("error", "Type UNDO to confirm."), request)
    c = conn()
    try:
        acc_id = c.execute("SELECT account_id FROM runs WHERE id=?", (run_id,)).fetchone()["account_id"]
        engine.undo_run(c, run_id, user(request))
    except ValueError as e:
        return redirect(f"/runs/{run_id}", ("error", str(e)), request)
    finally:
        c.close()
    return redirect(f"/history?account={acc_id}", ("ok", f"Run #{run_id} undone. You can now re-run that date."), request)


@app.post("/matches/{match_id}/unmatch")
def do_unmatch(request: Request, match_id: int, reason: str = Form(""), back: str = Form("/"),
               csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    back = safe_back(back)
    if not check_csrf(request, csrf_token):
        return redirect(back, ("error", "Session expired."), request)
    c = conn()
    try:
        engine.unmatch(c, match_id, user(request), reason)
    except ValueError as e:
        return redirect(back, ("error", str(e)), request)
    finally:
        c.close()
    return redirect(back, ("ok", "Match removed; the lines are open again."), request)


@app.post("/manual-match")
def do_manual_match(request: Request, account_id: int = Form(...), item_ids: str = Form(""),
                    remark: str = Form(""), back: str = Form("/"), csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    back = safe_back(back)
    if not check_csrf(request, csrf_token):
        return redirect(back, ("error", "Session expired."), request)
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    c = conn()
    try:
        engine.manual_match(c, account_id, ids, remark, user(request))
    except ValueError as e:
        return redirect(back, ("error", str(e)), request)
    finally:
        c.close()
    return redirect(back, ("ok", "Manual match saved."), request)


@app.post("/items/{item_id}/note")
def item_note(request: Request, item_id: int, note: str = Form(""), back: str = Form("/"),
              csrf_token: str = Form("")):
    if (g := guard(request)):
        return g
    back = safe_back(back)
    if not check_csrf(request, csrf_token):
        return redirect(back, ("error", "Session expired."), request)
    c = conn()
    try:
        engine.add_note(c, item_id, note, user(request))
    finally:
        c.close()
    return redirect(back, ("ok", "Note saved."), request)


# ---------------------------------------------------------------------------- open items / history / audit

@app.get("/open-items", response_class=HTMLResponse)
def open_items_page(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        accs = accounts_overview(c)
        sel = request.query_params.get("account")
        sel = int(sel) if sel and sel.isdigit() else next((a["id"] for a in accs if a.get("last")), None)
        items, acc, last = [], None, None
        if sel:
            acc = next((a for a in accs if a["id"] == sel), None)
            last = engine.latest_run(c, sel)
            if last:
                items = engine.open_items_as_of(c, sel, last["id"])
    finally:
        c.close()
    as_of = date.fromisoformat(last["period_to"]) if last else date.today()
    return render(request, "open_items.html", accounts=accs, account=acc, items=items,
                  last=dict(last) if last else None, as_of=as_of)


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        accs = [dict(a) for a in c.execute("SELECT * FROM accounts ORDER BY nickname")]
        sel = request.query_params.get("account")
        q = "SELECT r.*, a.nickname, a.acc_no, a.kind FROM runs r JOIN accounts a ON a.id=r.account_id"
        args = []
        if sel and sel.isdigit():
            q += " WHERE r.account_id=?"
            args.append(int(sel))
        q += " ORDER BY r.period_to DESC, r.id DESC LIMIT 500"
        runs = [dict(r) for r in c.execute(q, args)]
        latest_ids = {engine.latest_run(c, a["id"])["id"] for a in accs if engine.latest_run(c, a["id"])}
        for r in runs:
            r["brs"] = json.loads(r["brs_json"] or "{}")
            r["summary"] = json.loads(r["summary_json"] or "{}")
            r["is_latest"] = r["id"] in latest_ids
    finally:
        c.close()
    return render(request, "history.html", runs=runs, accounts=accs, sel=int(sel) if sel and sel.isdigit() else None)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 1000")]
    finally:
        c.close()
    return render(request, "audit.html", rows=rows)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------- sub-path mounting
if ROOT_PATH:
    inner = app
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount(ROOT_PATH, inner)

    @app.get("/healthz")
    def outer_healthz():
        return {"ok": True}

    @app.get("/")
    def outer_root():
        return RedirectResponse(ROOT_PATH + "/", status_code=307)
