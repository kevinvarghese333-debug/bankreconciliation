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

from . import accounts_import, auth, batch, db, engine, report, roles
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
# One-time (20-10-2026): the ICICI collection account (NACH, dynamic UPI); its balance is swept to SIB …1113.
try:
    if not db.get_setting(_c, "seed_icici_0993"):
        if not _c.execute("SELECT 1 FROM accounts WHERE acc_no LIKE '%0993'").fetchone() and \
                _c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            _c.execute("INSERT INTO accounts(bank, acc_no, nickname, kind, branch, notes, created_at) VALUES (?,?,?,?,?,?,?)",
                       ("ICICI Bank", "105305000993", "ICICI 0993 · NACH & UPI collections", "collection", "Head Office",
                        "Automated collections (NACH, dynamic UPI); balance transferred to SIB …1113", db.now()))
            db.audit(_c, "system", "account_added", "ICICI Bank 105305000993 (collection)")
        db.set_setting(_c, "seed_icici_0993", db.now())
except Exception as _e:
    print("icici seed skipped:", _e)
# One-time (03-10-2026): apply the new branch-name and loan-renewal rules to the …113 account's open items.
try:
    if not db.get_setting(_c, "recheck_2026_10_03"):
        for _a in _c.execute("SELECT id FROM accounts WHERE acc_no LIKE '%113' ORDER BY id").fetchall():
            engine.recheck_account(_c, _a["id"], "system")
        db.set_setting(_c, "recheck_2026_10_03", db.now())
except Exception as _e:  # never stop the app from starting
    print("re-check skipped:", _e)
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
    role = getattr(request.state, "role", None)
    ctx.update(request=request, user=user(request), csrf=csrf(request),
               flash=request.session.pop("flash", None), role=role,
               role_label=roles.LABELS.get(role, ""), full_name=getattr(request.state, "full_name", ""),
               can=lambda perm: roles.can(role, perm))
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, flash: tuple[str, str] | None = None, request: Request | None = None):
    if flash and request is not None:
        request.session["flash"] = {"kind": flash[0], "msg": flash[1]}
    if url.startswith("/"):
        url = ROOT_PATH + url
    return RedirectResponse(url, status_code=303)


def guard(request: Request, perm: str = "view"):
    """Returns a redirect if not logged in, disabled, or the role lacks `perm`; else None.

    `perm` "self" only needs a valid login (used for the change-password page)."""
    c = conn()
    try:
        if auth.user_count(c) == 0:
            return RedirectResponse(ROOT_PATH + "/setup", status_code=303)
        row = c.execute("SELECT * FROM users WHERE username=?", (user(request) or "",)).fetchone()
    finally:
        c.close()
    if not user(request):
        return RedirectResponse(ROOT_PATH + "/login", status_code=303)
    if (not row or not row["active"] or not row["session_nonce"]
            or not secrets.compare_digest(row["session_nonce"], request.session.get("nonce", ""))):
        request.session.clear()
        return RedirectResponse(ROOT_PATH + "/login", status_code=303)
    request.state.role = row["role"]
    request.state.full_name = row["full_name"] or row["username"]
    if perm == "self":
        return None
    if row["must_change_pw"]:
        return redirect("/me", ("error", "Set your own password before continuing."), request)
    if not roles.can(row["role"], perm):
        return redirect("/", ("error", f"Your role ({roles.LABELS.get(row['role'], row['role'])}) cannot "
                                       f"{roles.ACTION_TEXT.get(perm, perm)}. Ask an Admin."), request)
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
        c.execute("INSERT INTO users(username, pw_hash, created_at, role, full_name) VALUES (?,?,?,?,?)",
                  (username.strip(), auth.hash_pw(password), db.now(), "admin", username.strip()))
        db.audit(c, username.strip(), "setup", "Admin login created")
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
        if not row["active"]:
            db.audit(c, row["username"], "login_blocked", f"disabled user, from {ip}")
            return redirect("/login", ("error", "This user is disabled. Contact your Admin."), request)
        auth.clear_fails(ip)
        db.audit(c, row["username"], "login", f"from {ip}")
        c.execute("UPDATE users SET last_login=? WHERE id=?", (db.now(), row["id"]))
        _start_session(request, c, row["username"])
    finally:
        c.close()
    if row["must_change_pw"]:
        return redirect("/me", ("ok", "Welcome. Please set your own password to continue."), request)
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


@app.get("/me", response_class=HTMLResponse)
def me_page(request: Request):
    if (g := guard(request, "self")):
        return g
    c = conn()
    try:
        me = dict(c.execute("SELECT username, full_name, role, must_change_pw, last_login, created_at FROM users "
                            "WHERE username=?", (user(request),)).fetchone())
    finally:
        c.close()
    me["role_label"] = roles.LABELS.get(me["role"], me["role"])
    me["role_desc"] = roles.DESCRIPTIONS.get(me["role"], "")
    return render(request, "me.html", me=me)


@app.post("/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), new2: str = Form(...),
                    csrf_token: str = Form("")):
    if (g := guard(request, "self")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/me", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        row = c.execute("SELECT * FROM users WHERE username=?", (user(request),)).fetchone()
        if not auth.verify_pw(current, row["pw_hash"]):
            return redirect("/me", ("error", "Current password is wrong."), request)
        if len(new) < 10 or new != new2:
            return redirect("/me", ("error", "New password must be 10+ characters and match."), request)
        if new == current:
            return redirect("/me", ("error", "Choose a password different from the current one."), request)
        c.execute("UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?", (auth.hash_pw(new), row["id"]))
        db.audit(c, user(request), "password_changed")
        _start_session(request, c, row["username"])   # signs out every other session
    finally:
        c.close()
    return redirect("/", ("ok", "Password changed."), request)


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
        from datetime import timedelta
        yday = (date.today() - timedelta(days=1)).isoformat()
        active = [a for a in accs if a["active"]]
        st = {"accounts": len(active), "pending": [], "crit": 0, "high": 0, "unreviewed": 0, "unreviewed_run": None,
              "unsigned": [], "tallied": 0, "untallied": [], "open": 0, "oldest": None, "yday": yday}
        for a in active:
            last = a["last"]
            if not last or last["period_to"] < yday:
                st["pending"].append(a)
            if not last:
                continue
            sv = a["summary"].get("severity", {})
            st["crit"] += sv.get("critical", 0)
            st["high"] += sv.get("high", 0)
            flags = engine.attach_reviews(c, last, json.loads(last["flags_json"] or "[]"))
            unrev = sum(1 for f in flags if not f["review"] and f["severity"] in ("critical", "high"))
            if unrev and not st["unreviewed_run"]:
                st["unreviewed_run"] = last["id"]
            st["unreviewed"] += unrev
            if not last.get("checked_by"):
                st["unsigned"].append(a)
            if a["brs"].get("clean"):
                st["tallied"] += 1
            else:
                st["untallied"].append(a)
            st["open"] += a.get("open_count", 0)
            if a.get("oldest_open") and (not st["oldest"] or a["oldest_open"] < st["oldest"]):
                st["oldest"] = a["oldest_open"]
    finally:
        c.close()
    return render(request, "dashboard.html", accounts=accs, recent=recent, st=st)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request):
    """Find any bank line or bank book entry by customer name, UTR / reference, voucher or amount."""
    if (g := guard(request)):
        return g
    q = (request.query_params.get("q") or "").strip()
    rows = []
    if len(q) >= 3:
        c = conn()
        try:
            amt = None
            try:
                amt = round(float(q.replace(",", "").replace("₹", "")), 2)
            except ValueError:
                pass
            like = f"%{q.upper()}%"
            sql = ("SELECT i.*, a.nickname, a.acc_no, m.method, m.confidence, m.run_id AS match_run "
                   "FROM items i JOIN accounts a ON a.id=i.account_id LEFT JOIN matches m ON m.id=i.match_id "
                   "WHERE UPPER(i.name) LIKE ? OR UPPER(i.ref) LIKE ? OR UPPER(i.data_json) LIKE ?")
            args = [like, like, like]
            if amt is not None:
                sql += " OR ABS(i.amount - ?) < 0.005"
                args.append(amt)
            sql += " ORDER BY i.date DESC, i.id DESC LIMIT 200"
            for r in c.execute(sql, args):
                it = engine.row_to_item(r)
                it.update(nickname=r["nickname"], acc_no=r["acc_no"], method=r["method"], confidence=r["confidence"])
                rows.append(it)
        finally:
            c.close()
    return render(request, "search.html", q=q, rows=rows)


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
    if (g := guard(request, "accounts")):
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
    if (g := guard(request, "accounts")):
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
        if d.is_dir() and time.time() - d.stat().st_mtime > 12 * 3600:
            shutil.rmtree(d, ignore_errors=True)


@app.get("/reconcile", response_class=HTMLResponse)
def reconcile_form(request: Request):
    if (g := guard(request, "reconcile")):
        return g
    c = conn()
    try:
        accs = accounts_overview(c)
    finally:
        c.close()
    pre = request.query_params.get("account")
    return render(request, "reconcile.html", accounts=[a for a in accs if a["active"]],
                  preselect=int(pre) if pre and pre.isdigit() else None)


def _stage_many(uploads, folder: Path):
    """Stage one or more files. One file → (path, name); several → ([paths], 'a + b'); none → (None, '')."""
    ups = [u for u in (uploads or []) if u is not None and u.filename]
    if not ups:
        return None, ""
    staged = [_stage(u, folder) for u in ups]
    if len(staged) == 1:
        return str(staged[0][0]), staged[0][1]
    return [str(p) for p, _ in staged], " + ".join(n for _, n in staged)


def _finish_upload(request, token, meta, acknowledged=False, reason=""):
    folder = STAGING / token
    c = conn()
    try:
        try:
            rid = engine.reconcile(c, meta["account_id"], meta["bank_path"], meta["bank_name"], meta["book_path"],
                                   meta["book_name"], user(request), acknowledged, reason,
                                   backdated_path=meta.get("backdated_path"), backdated_name=meta.get("backdated_name", ""),
                                   confirmed=bool(meta.get("confirmed")))
        except engine.Blocked as e:
            acc = c.execute("SELECT * FROM accounts WHERE id=?", (meta["account_id"],)).fetchone()
            last = engine.latest_run(c, meta["account_id"])
            book_gap = any(ch["code"] == "BOOK_CONTINUITY" and ch["level"] != "ok" for ch in e.checks)
            return render(request, "blocked.html", checks=e.checks, ackable=e.ackable, token=token,
                          account=dict(acc), meta=meta, book_gap=book_gap,
                          last_to=last["period_to"] if last else None)
        except Exception as e:  # unreadable file etc.
            shutil.rmtree(folder, ignore_errors=True)
            return redirect("/reconcile", ("error", f"Could not process the files: {e}"), request)
    finally:
        c.close()
    if meta.get("batch"):
        _batch_mark(meta["batch"], meta.get("pairkey", ""), {"status": "done", "run_id": rid, "message": ""})
    if KEEP_UPLOADS:
        keep = db.DATA_DIR / "uploads" / str(rid)
        keep.mkdir(parents=True, exist_ok=True)
        shutil.copy(meta["bank_path"], keep / meta["bank_name"])
        kp = meta["book_path"] if isinstance(meta["book_path"], list) else [meta["book_path"]]
        kn = meta["book_name"].split(" + ") if len(kp) > 1 else [meta["book_name"]]
        for src, nm in zip(kp, kn if len(kn) == len(kp) else [Path(x).name for x in kp]):
            shutil.copy(src, keep / nm)
    shutil.rmtree(folder, ignore_errors=True)
    if meta.get("batch"):
        return redirect(f"/reconcile/batch/{meta['batch']}", ("ok", f"Run #{rid} reconciled. Continue with the rest below."),
                        request)
    return redirect(f"/runs/{rid}", ("ok", "Reconciliation complete."), request)


@app.post("/reconcile")
def reconcile(request: Request, account_id: int = Form(...), bank_file: UploadFile = File(...),
              book_file: list[UploadFile] = File(...), backdated_file: list[UploadFile] | None = File(None),
              confirm_backdated: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "reconcile")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/reconcile", ("error", "Session expired, try again."), request)
    if confirm_backdated != "1":
        return redirect("/reconcile", ("error", "Please confirm that all back-dated entries are posted and that there "
                                                "will be no more back-dated entries after downloading the report."), request)
    _cleanup_staging()
    token = secrets.token_hex(12)
    folder = STAGING / token
    folder.mkdir(parents=True, exist_ok=True)
    try:
        bpath, bname = _stage(bank_file, folder)
        kpath, kname = _stage_many(book_file, folder)
        dpath, dname = _stage_many(backdated_file or [], folder)
    except ValueError as e:
        shutil.rmtree(folder, ignore_errors=True)
        return redirect("/reconcile", ("error", str(e)), request)
    meta = {"account_id": account_id, "bank_path": str(bpath), "bank_name": bname,
            "book_path": kpath, "book_name": kname, "user": user(request), "confirmed": True,
            "backdated_path": dpath, "backdated_name": dname}
    (folder / "meta.json").write_text(json.dumps(meta))
    return _finish_upload(request, token, meta)


@app.post("/reconcile/backdated")
def reconcile_backdated(request: Request, token: str = Form(...), backdated_file: list[UploadFile] = File(...),
                        csrf_token: str = Form("")):
    """Stop page → attach a fresh bank book of the earlier day(s) to find back-dated entries, then re-run."""
    if (g := guard(request, "reconcile")):
        return g
    if not check_csrf(request, csrf_token) or not token.isalnum():
        return redirect("/reconcile", ("error", "Session expired, upload again."), request)
    folder = STAGING / token
    mp = folder / "meta.json"
    if not mp.exists():
        return redirect("/reconcile", ("error", "Upload expired (1 hour). Please upload again."), request)
    meta = json.loads(mp.read_text())
    if meta.get("user") != user(request):
        return redirect("/reconcile", ("error", "Upload belongs to another session."), request)
    try:
        dpath, dname = _stage_many(backdated_file, folder)
    except ValueError as e:
        return redirect("/reconcile", ("error", str(e)), request)
    meta.update(backdated_path=dpath, backdated_name=dname)
    mp.write_text(json.dumps(meta))
    return _finish_upload(request, token, meta)


# ---------------------------------------------------------------------------- bulk upload (many accounts at once)

def _batch_dir(bid: str) -> Path:
    if not bid.isalnum():
        raise ValueError("bad batch id")
    return STAGING / f"batch_{bid}"


def _batch_load(bid: str) -> dict | None:
    p = _batch_dir(bid) / "batch.json"
    return json.loads(p.read_text()) if p.exists() else None


def _batch_save(b: dict) -> None:
    d = _batch_dir(b["id"])
    (d / "batch.json").write_text(json.dumps(b, default=str))
    os.utime(d, None)


def _batch_mark(bid: str, key: str, result: dict) -> None:
    b = _batch_load(bid)
    if b is not None and key:
        b.setdefault("results", {})[key] = result
        _batch_save(b)


def _batch_add(c, b: dict, uploads: list[UploadFile]) -> list[str]:
    errors = []
    accounts = [dict(a) for a in c.execute("SELECT * FROM accounts WHERE active=1")]
    d = _batch_dir(b["id"])
    for up in uploads:
        if not up or not up.filename:
            continue
        try:
            path, name = _stage(up, d)
        except ValueError as e:
            errors.append(str(e))
            continue
        info = batch.classify(str(path), name)
        f = {"fid": secrets.token_hex(5), "name": name, "path": str(path), "kind": info["kind"],
             "date_from": info["date_from"].isoformat() if info["date_from"] else None,
             "date_to": info["date_to"].isoformat() if info["date_to"] else None,
             "error": info["error"], "account_id": None, "how": ""}
        if info["kind"] != "unknown":
            f["account_id"], f["how"] = batch.identify(c, info, accounts)
            f.update({k: info[k] for k in ("utrs", "nach", "receipts", "cands") if k in info})
        else:
            f["how"] = info["error"] or "Not a bank statement or an ERP bank book."
        b["files"].append(f)
    batch.resolve_by_content(b["files"])
    return errors


def _batch_run(c, b: dict, username: str) -> None:
    """Reconcile every complete pair that is not done yet, oldest day first per account."""
    pl = batch.plan(b["files"])
    files = {f["fid"]: f for f in b["files"]}
    res = b.setdefault("results", {})
    blocked_accounts = set()
    for p in pl["pairs"]:
        key = f"{p['stmt']}|{p['book']}"
        r = res.get(key, {})
        if r.get("status") in ("done", "already"):
            continue
        if p["account_id"] in blocked_accounts:
            res[key] = {"status": "waiting", "message": "Waiting for the earlier day of this account."}
            continue
        s, ks = files[p["stmt"]], [files[x] for x in p.get("books") or [p["book"]]]
        k = ks[0] if len(ks) == 1 else {"path": [x["path"] for x in ks], "name": " + ".join(x["name"] for x in ks)}
        token = secrets.token_hex(12)
        (STAGING / token).mkdir(parents=True, exist_ok=True)
        meta = {"account_id": p["account_id"], "bank_path": s["path"], "bank_name": s["name"],
                "book_path": k["path"], "book_name": k["name"], "user": username, "confirmed": True,
                "backdated_path": None, "backdated_name": "", "batch": b["id"], "pairkey": key}
        (STAGING / token / "meta.json").write_text(json.dumps(meta))
        try:
            rid = engine.reconcile(c, p["account_id"], s["path"], s["name"], k["path"], k["name"], username,
                                   confirmed=True)
            res[key] = {"status": "done", "run_id": rid, "message": ""}
            shutil.rmtree(STAGING / token, ignore_errors=True)
        except engine.Blocked as e:
            done = [ch for ch in e.checks if ch["code"] == "ALREADY_DONE"]
            if done and done[0].get("run_id"):
                res[key] = {"status": "already", "run_id": done[0]["run_id"], "message": "Already reconciled."}
                shutil.rmtree(STAGING / token, ignore_errors=True)
                continue
            msgs = [ch["message"] for ch in e.checks if ch["level"] == "error"] or [ch["message"] for ch in e.ackable]
            res[key] = {"status": "confirm" if e.ackable and all(ch in e.ackable for ch in e.checks if ch["level"] == "error")
                        else "stopped", "message": " · ".join(m[:220] for m in msgs[:2]), "token": token}
            blocked_accounts.add(p["account_id"])
        except Exception as e:  # unreadable file etc.
            res[key] = {"status": "stopped", "message": f"Could not process the files: {e}"}
            blocked_accounts.add(p["account_id"])
    b["plan"] = {"pairs": pl["pairs"], "lonely": [x["fid"] for x in pl["lonely"]],
                 "needs": {x["fid"]: x["needs"] for x in pl["lonely"]}, "dupes": [x["fid"] for x in pl["dupes"]]}


@app.post("/reconcile/batch")
def batch_start(request: Request, files: list[UploadFile] = File(...), confirm_backdated: str = Form(""),
                csrf_token: str = Form("")):
    if (g := guard(request, "reconcile")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/reconcile", ("error", "Session expired, try again."), request)
    if confirm_backdated != "1":
        return redirect("/reconcile", ("error", "Please confirm that all back-dated entries are posted and that there "
                                                "will be no more back-dated entries after downloading the reports."), request)
    _cleanup_staging()
    bid = secrets.token_hex(8)
    _batch_dir(bid).mkdir(parents=True, exist_ok=True)
    b = {"id": bid, "user": user(request), "created": db.now(), "files": [], "results": {}}
    c = conn()
    try:
        errs = _batch_add(c, b, files)
        _batch_run(c, b, user(request))
        db.audit(c, user(request), "batch_upload", f"{len(b['files'])} file(s); confirmed back-dated entries posted")
    finally:
        c.close()
    _batch_save(b)
    return redirect(f"/reconcile/batch/{bid}", ("error", "; ".join(errs)) if errs else None, request)


def _batch_guard(request: Request, bid: str):
    if (g := guard(request, "reconcile")):
        return g, None
    try:
        b = _batch_load(bid)
    except ValueError:
        b = None
    if not b:
        return redirect("/reconcile", ("error", "That upload has expired (12 hours). Please upload again."), request), None
    if b["user"] != user(request):
        return redirect("/reconcile", ("error", "That upload belongs to another user."), request), None
    return None, b


@app.get("/reconcile/batch/{bid}", response_class=HTMLResponse)
def batch_view(request: Request, bid: str):
    g, b = _batch_guard(request, bid)
    if g:
        return g
    c = conn()
    try:
        accounts = {a["id"]: dict(a) for a in c.execute("SELECT * FROM accounts")}
        for a in accounts.values():
            lr = engine.latest_run(c, a["id"])
            a["last_to"] = lr["period_to"] if lr else None
    finally:
        c.close()
    files = {f["fid"]: f for f in b["files"]}
    pl = b.get("plan") or {"pairs": [], "lonely": [], "needs": {}, "dupes": []}
    rows = []
    for p in pl["pairs"]:
        r = b.get("results", {}).get(f"{p['stmt']}|{p['book']}", {"status": "pending"})
        bks = [files[x] for x in p.get("books") or [p["book"]]]
        book_f = bks[0] if len(bks) == 1 else {"name": " + ".join(x["name"] for x in bks), "how": bks[0]["how"]}
        rows.append(dict(p, acc=accounts.get(p["account_id"]), stmt_f=files[p["stmt"]], book_f=book_f, **r))
    lonely = [dict(files[fid], needs=pl["needs"].get(fid), acc=accounts.get(files[fid]["account_id"]))
              for fid in pl["lonely"]]
    unknown = [f for f in b["files"] if not f["account_id"]]
    dupes = [files[fid] for fid in pl["dupes"]]
    days = sorted({p["date"] for p in pl["pairs"] if p["date"]} | {x["date_from"] for x in lonely if x.get("date_from")})
    covered = {(p["account_id"], p["date"]) for p in pl["pairs"]}
    partial = {(x["account_id"], x["date_from"]) for x in lonely}
    still = []
    for d in days:
        for a in accounts.values():
            if a["active"] and (a["id"], d) not in covered and (a["id"], d) not in partial \
                    and not (a["last_to"] and a["last_to"] >= d):
                still.append({"date": d, "acc": a})
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("done", "already", "confirm", "stopped", "waiting", "pending")}
    return render(request, "batch.html", b=b, rows=rows, lonely=lonely, unknown=unknown, dupes=dupes, still=still,
                  days=days, counts=counts, accounts=sorted((a for a in accounts.values() if a["active"]),
                                                            key=lambda a: a["nickname"]))


@app.post("/reconcile/batch/{bid}/add")
def batch_add_files(request: Request, bid: str, files: list[UploadFile] = File(...), csrf_token: str = Form("")):
    g, b = _batch_guard(request, bid)
    if g:
        return g
    if not check_csrf(request, csrf_token):
        return redirect(f"/reconcile/batch/{bid}", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        errs = _batch_add(c, b, files)
        _batch_run(c, b, user(request))
    finally:
        c.close()
    _batch_save(b)
    return redirect(f"/reconcile/batch/{bid}", ("error", "; ".join(errs)) if errs else ("ok", "Files added."), request)


@app.post("/reconcile/batch/{bid}/assign")
def batch_assign(request: Request, bid: str, fid: str = Form(...), account_id: str = Form(""), csrf_token: str = Form("")):
    g, b = _batch_guard(request, bid)
    if g:
        return g
    if not check_csrf(request, csrf_token):
        return redirect(f"/reconcile/batch/{bid}", ("error", "Session expired, try again."), request)
    f = next((x for x in b["files"] if x["fid"] == fid), None)
    if f and account_id.isdigit():
        f["account_id"], f["how"] = int(account_id), "chosen by " + user(request)
        c = conn()
        try:
            _batch_run(c, b, user(request))
        finally:
            c.close()
        _batch_save(b)
    return redirect(f"/reconcile/batch/{bid}", ("ok", f"Assigned {f['name']}.") if f else None, request)


@app.post("/reconcile/batch/{bid}/run")
def batch_rerun(request: Request, bid: str, csrf_token: str = Form("")):
    g, b = _batch_guard(request, bid)
    if g:
        return g
    if not check_csrf(request, csrf_token):
        return redirect(f"/reconcile/batch/{bid}", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        _batch_run(c, b, user(request))
    finally:
        c.close()
    _batch_save(b)
    return redirect(f"/reconcile/batch/{bid}", ("ok", "Ran the remaining reconciliations again."), request)


@app.get("/reconcile/resume/{token}")
def reconcile_resume(request: Request, token: str):
    """Open one stopped reconciliation from a bulk upload (acknowledge, find back-dated entries, or cancel)."""
    if (g := guard(request, "reconcile")):
        return g
    mp = STAGING / token / "meta.json"
    if not token.isalnum() or not mp.exists():
        return redirect("/reconcile", ("error", "That upload has expired. Please upload again."), request)
    meta = json.loads(mp.read_text())
    if meta.get("user") != user(request):
        return redirect("/reconcile", ("error", "Upload belongs to another session."), request)
    return _finish_upload(request, token, meta)


@app.post("/reconcile/ack")
def reconcile_ack(request: Request, token: str = Form(...), reason: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "reconcile")):
        return g
    if not check_csrf(request, csrf_token) or not token.isalnum():
        return redirect("/reconcile", ("error", "Session expired, upload again."), request)
    mp = STAGING / token / "meta.json"
    if not mp.exists():
        return redirect("/reconcile", ("error", "Upload expired (1 hour). Please upload again."), request)
    meta = json.loads(mp.read_text())
    if meta.get("user") != user(request):
        return redirect("/reconcile", ("error", "Upload belongs to another session."), request)
    if not roles.can(request.state.role, "ack"):
        return redirect("/reconcile", ("error", "Only a Manager or Admin can acknowledge warnings."), request)
    return _finish_upload(request, token, meta, True, reason)


@app.post("/reconcile/cancel")
def reconcile_cancel(request: Request, token: str = Form(...), csrf_token: str = Form("")):
    if (g := guard(request, "reconcile")):
        return g
    if check_csrf(request, csrf_token) and token.isalnum():
        mp = STAGING / token / "meta.json"
        meta = json.loads(mp.read_text()) if mp.exists() else {}
        shutil.rmtree(STAGING / token, ignore_errors=True)
        if meta.get("batch"):
            _batch_mark(meta["batch"], meta.get("pairkey", ""),
                        {"status": "stopped", "message": "Cancelled. Upload corrected files with 'Add more files'."})
            return redirect(f"/reconcile/batch/{meta['batch']}")
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
    c = conn()
    try:
        engine.attach_reviews(c, run, flags)
    finally:
        c.close()
    for f in flags:
        f["keyset"] = ",".join(f["keys"])
    return render(request, "run.html", run=run, account=acc,
                  checks=json.loads(run["checks_json"] or "[]"), flags=flags,
                  brs=json.loads(run["brs_json"] or "{}"), s=json.loads(run["summary_json"] or "{}"),
                  groups=_group_matches(items, matches), open_items=open_,
                  is_latest=latest and latest["id"] == run_id,
                  as_of=date.fromisoformat(run["period_to"]), review_status=engine.REVIEW_STATUS)


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


@app.post("/runs/{run_id}/review")
def run_review(request: Request, run_id: int, subject: str = Form(...), status: str = Form(...),
               remark: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "note")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect(f"/runs/{run_id}", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        engine.save_review(c, run_id, subject, status, remark, user(request))
    except ValueError as e:
        return redirect(f"/runs/{run_id}#flags", ("error", str(e)), request)
    finally:
        c.close()
    return redirect(f"/runs/{run_id}#flags", ("ok", "Review saved."), request)


@app.post("/runs/{run_id}/signoff")
def run_signoff(request: Request, run_id: int, note: str = Form(""), undo: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "signoff")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect(f"/runs/{run_id}", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return redirect("/history", ("error", "Run not found."), request)
        if undo:
            if run["checked_by"] != user(request) and not roles.can(request.state.role, "admin"):
                return redirect(f"/runs/{run_id}", ("error", "Only the person who signed off, or an Admin, can withdraw it."), request)
            c.execute("UPDATE runs SET checked_by='', checked_at='', checked_note='' WHERE id=?", (run_id,))
            db.audit(c, user(request), "signoff_withdrawn", f"Run #{run_id}")
            return redirect(f"/runs/{run_id}", ("ok", "Sign-off withdrawn."), request)
        if run["checked_by"]:
            return redirect(f"/runs/{run_id}", ("error", f"Already signed off by {run['checked_by']}."), request)
        if run["created_by"] == user(request):
            return redirect(f"/runs/{run_id}", ("error", "You reconciled this run, so someone else must check it."), request)
        c.execute("UPDATE runs SET checked_by=?, checked_at=?, checked_note=? WHERE id=?",
                  (user(request), db.now(), note.strip()[:200], run_id))
        db.audit(c, user(request), "signoff", f"Run #{run_id} ({run['period_to']})" + (f" · {note.strip()}" if note.strip() else ""))
    finally:
        c.close()
    return redirect(f"/runs/{run_id}", ("ok", "Signed off as checked."), request)


@app.post("/runs/{run_id}/undo")
def run_undo(request: Request, run_id: int, confirm: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "undo")):
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
    if (g := guard(request, "unmatch")):
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
    if (g := guard(request, "match")):
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
    if (g := guard(request, "note")):
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


# ---------------------------------------------------------------------------- bank holidays

def _parse_holiday_lines(text: str):
    """Lines like '02-10-2026 Gandhi Jayanthi', '2026-10-02, Gandhi Jayanthi' or '02/10/2026'."""
    from .parsers import to_date
    good, bad = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[,\t]+|\s+", line, maxsplit=1)
        d = to_date(parts[0].strip())
        if d is None and len(line.split()) >= 3:          # "2 Oct 2026 Gandhi Jayanthi"
            d = to_date(" ".join(line.split()[:3]))
            parts = [" ".join(line.split()[:3]), " ".join(line.split()[3:])]
        if d is None or not (2020 <= d.year <= 2100):
            bad.append(line)
            continue
        good.append((d, (parts[1] if len(parts) > 1 else "").strip(" ,-\t")[:80]))
    return good, bad


@app.get("/holidays", response_class=HTMLResponse)
def holidays_page(request: Request):
    if (g := guard(request)):
        return g
    c = conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM holidays ORDER BY day DESC")]
    finally:
        c.close()
    for r in rows:
        r["date"] = date.fromisoformat(r["day"])
    return render(request, "holidays.html", rows=rows, today=date.today())


@app.post("/holidays")
def holidays_add(request: Request, lines: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "holidays")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/holidays", ("error", "Session expired, try again."), request)
    good, bad = _parse_holiday_lines(lines)
    if not good:
        return redirect("/holidays", ("error", "No dates found. Write one holiday per line, e.g. 02-10-2026 Gandhi Jayanthi."
                                      + (f" Could not read: {'; '.join(bad[:5])}" if bad else "")), request)
    c = conn()
    try:
        with db.tx(c):
            for d, name in good:
                c.execute("INSERT INTO holidays(day, name, created_at, created_by) VALUES (?,?,?,?) "
                          "ON CONFLICT(day) DO UPDATE SET name=excluded.name",
                          (d.isoformat(), name, db.now(), user(request)))
            db.audit(c, user(request), "holidays_added", "; ".join(f"{d:%d-%m-%Y} {n}".strip() for d, n in good))
            engine.refresh_latest_runs(c)
    finally:
        c.close()
    msg = f"{len(good)} holiday(s) saved."
    if bad:
        msg += f" Skipped {len(bad)} line(s) that had no readable date: {'; '.join(bad[:5])}"
    return redirect("/holidays", ("ok" if not bad else "error", msg), request)


@app.post("/holidays/delete")
def holidays_delete(request: Request, day: str = Form(...), csrf_token: str = Form("")):
    if (g := guard(request, "holidays")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/holidays", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        with db.tx(c):
            row = c.execute("SELECT * FROM holidays WHERE day=?", (day,)).fetchone()
            if row:
                c.execute("DELETE FROM holidays WHERE day=?", (day,))
                db.audit(c, user(request), "holiday_removed", f"{row['day']} {row['name']}")
                engine.refresh_latest_runs(c)
    finally:
        c.close()
    return redirect("/holidays", ("ok", "Holiday removed."), request)


# ---------------------------------------------------------------------------- admin portal (/admin)

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


def _active_admins(c, exclude_id: int | None = None) -> int:
    q = "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
    args = []
    if exclude_id is not None:
        q += " AND id<>?"
        args.append(exclude_id)
    return c.execute(q, args).fetchone()[0]


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    if (g := guard(request, "admin")):
        return g
    c = conn()
    try:
        users = [dict(u) for u in c.execute(
            "SELECT id, username, full_name, role, active, must_change_pw, last_login, created_at FROM users "
            "ORDER BY active DESC, CASE role WHEN 'admin' THEN 0 WHEN 'manager' THEN 1 WHEN 'operator' THEN 2 ELSE 3 END, username")]
        stats = {
            "accounts": c.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0],
            "runs": c.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "revoked": c.execute("SELECT COUNT(*) FROM revoked_runs").fetchone()[0],
        }
    finally:
        c.close()
    for u in users:
        u["role_label"] = roles.LABELS.get(u["role"], u["role"])
    return render(request, "admin_users.html", users=users, stats=stats, roles_list=roles.ROLES[::-1],
                  role_labels=roles.LABELS, role_desc=roles.DESCRIPTIONS, tab="users")


@app.post("/admin/users")
def admin_user_create(request: Request, username: str = Form(...), full_name: str = Form(""), role: str = Form(...),
                      password: str = Form(...), csrf_token: str = Form("")):
    if (g := guard(request, "admin")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/admin", ("error", "Session expired, try again."), request)
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        return redirect("/admin", ("error", "Username: 3–32 characters, lowercase letters, digits, dot, dash or underscore."), request)
    if role not in roles.ROLES:
        return redirect("/admin", ("error", "Pick a role."), request)
    if len(password) < 10:
        return redirect("/admin", ("error", "Temporary password must be at least 10 characters."), request)
    c = conn()
    try:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return redirect("/admin", ("error", f"User '{username}' already exists."), request)
        c.execute("INSERT INTO users(username, pw_hash, created_at, role, full_name, active, must_change_pw) "
                  "VALUES (?,?,?,?,?,1,1)", (username, auth.hash_pw(password), db.now(), role, full_name.strip()[:80]))
        db.audit(c, user(request), "user_created", f"{username} ({roles.LABELS[role]}) {full_name.strip()}")
    finally:
        c.close()
    return redirect("/admin", ("ok", f"User '{username}' created as {roles.LABELS[role]}. Give them the temporary "
                                     "password; they must set their own at first sign-in."), request)


@app.post("/admin/users/{uid}")
def admin_user_update(request: Request, uid: int, full_name: str = Form(""), role: str = Form(...),
                      active: str = Form("1"), csrf_token: str = Form("")):
    if (g := guard(request, "admin")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/admin", ("error", "Session expired, try again."), request)
    if role not in roles.ROLES:
        return redirect("/admin", ("error", "Unknown role."), request)
    c = conn()
    try:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return redirect("/admin", ("error", "User not found."), request)
        act = 1 if active == "1" else 0
        if u["username"] == user(request) and (role != u["role"] or not act):
            return redirect("/admin", ("error", "You cannot change your own role or disable yourself. "
                                                "Ask another Admin."), request)
        if u["role"] == "admin" and (role != "admin" or not act) and _active_admins(c, exclude_id=uid) == 0:
            return redirect("/admin", ("error", "At least one active Admin must remain."), request)
        changes = []
        if role != u["role"]:
            changes.append(f"role {roles.LABELS.get(u['role'])} → {roles.LABELS[role]}")
        if act != u["active"]:
            changes.append("enabled" if act else "disabled")
        if full_name.strip() != (u["full_name"] or ""):
            changes.append(f"name '{full_name.strip()}'")
        nonce = u["session_nonce"] if act and role == u["role"] else secrets.token_urlsafe(24)   # sign out on change
        c.execute("UPDATE users SET full_name=?, role=?, active=?, session_nonce=? WHERE id=?",
                  (full_name.strip()[:80], role, act, nonce, uid))
        if changes:
            db.audit(c, user(request), "user_updated", f"{u['username']}: " + ", ".join(changes))
    finally:
        c.close()
    return redirect("/admin", ("ok", f"Saved {u['username']}." + (" They have been signed out." if nonce != u["session_nonce"] else "")), request)


@app.post("/admin/users/{uid}/reset")
def admin_user_reset(request: Request, uid: int, password: str = Form(...), csrf_token: str = Form("")):
    if (g := guard(request, "admin")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/admin", ("error", "Session expired, try again."), request)
    if len(password) < 10:
        return redirect("/admin", ("error", "Temporary password must be at least 10 characters."), request)
    c = conn()
    try:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return redirect("/admin", ("error", "User not found."), request)
        if u["username"] == user(request):
            return redirect("/admin", ("error", "Change your own password from My account."), request)
        c.execute("UPDATE users SET pw_hash=?, must_change_pw=1, session_nonce=? WHERE id=?",
                  (auth.hash_pw(password), secrets.token_urlsafe(24), uid))
        db.audit(c, user(request), "user_password_reset", u["username"])
    finally:
        c.close()
    return redirect("/admin", ("ok", f"Temporary password set for {u['username']}. They were signed out and must "
                                     "choose a new password at next sign-in."), request)


def _revoke_accounts(c, account: str) -> list[int]:
    if account == "all":
        return [r["id"] for r in c.execute("SELECT id FROM accounts ORDER BY id")]
    return [int(account)] if account.isdigit() else []


@app.get("/admin/revoke", response_class=HTMLResponse)
def admin_revoke_page(request: Request):
    if (g := guard(request, "admin")):
        return g
    account = request.query_params.get("account", "")
    from_s = request.query_params.get("from", "")
    c = conn()
    try:
        accounts = [dict(a) for a in c.execute("SELECT * FROM accounts ORDER BY kind DESC, nickname")]
        for a in accounts:
            lr = engine.latest_run(c, a["id"])
            a["last_period"] = lr["period_to"] if lr else None
        preview, from_d = None, None
        if account and from_s:
            try:
                from_d = date.fromisoformat(from_s)
            except ValueError:
                from_d = None
            if from_d:
                preview = engine.runs_to_revoke(c, _revoke_accounts(c, account), from_d)
        revoked = [dict(r) for r in c.execute(
            """SELECT v.id, v.run_id, v.period_from, v.period_to, v.created_by, v.revoked_at, v.revoked_by, v.reason,
                      a.nickname FROM revoked_runs v LEFT JOIN accounts a ON a.id=v.account_id
               ORDER BY v.id DESC LIMIT 300""")]
    finally:
        c.close()
    return render(request, "admin_revoke.html", accounts=accounts, account=account, from_s=from_s, from_d=from_d,
                  preview=preview, revoked=revoked, tab="revoke")


@app.post("/admin/revoke")
def admin_revoke(request: Request, account: str = Form(...), from_date: str = Form(...), reason: str = Form(""),
                 confirm: str = Form(""), csrf_token: str = Form("")):
    if (g := guard(request, "admin")):
        return g
    back = f"/admin/revoke?account={account}&from={from_date}"
    if not check_csrf(request, csrf_token):
        return redirect(back, ("error", "Session expired, try again."), request)
    if confirm.strip().upper() != "REVOKE":
        return redirect(back, ("error", "Type REVOKE to confirm."), request)
    try:
        d = date.fromisoformat(from_date)
    except ValueError:
        return redirect("/admin/revoke", ("error", "Pick a valid date."), request)
    c = conn()
    try:
        runs = engine.revoke_from(c, _revoke_accounts(c, account), d, reason, user(request))
    except ValueError as e:
        return redirect(back, ("error", str(e)), request)
    finally:
        c.close()
    return redirect("/admin/revoke", ("ok", f"Revoked {len(runs)} reconciliation(s) from {d:%d-%m-%Y}. Those days can "
                                            "now be reconciled again, in order."), request)


@app.post("/admin/recheck")
def admin_recheck(request: Request, account: str = Form(...), csrf_token: str = Form("")):
    if (g := guard(request, "admin")):
        return g
    if not check_csrf(request, csrf_token):
        return redirect("/admin/revoke", ("error", "Session expired, try again."), request)
    c = conn()
    try:
        out = [engine.recheck_account(c, aid, user(request)) for aid in _revoke_accounts(c, account)]
    finally:
        c.close()
    n = sum(o["new_matches"] for o in out)
    detail = "; ".join(f"{o['account']}: {o['new_matches']} new, open {o['open_before']} → {o['open_after']}"
                       for o in out if o["new_matches"]) or "nothing new to match"
    return redirect("/admin/revoke", ("ok", f"Re-check done — {n} new match(es). {detail}."), request)


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    if (g := guard(request, "audit")):
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
