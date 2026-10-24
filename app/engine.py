"""Reconciliation workflow: parse -> validate -> store -> match -> flags -> BRS.

All state lives in the database so that open items carry forward day to day,
and every run can be reproduced or undone (latest first, in order).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import db
from .matcher import Matcher, build_flags, compute_brs, find_subset, TOL, BIG_CREDIT, named_in_narration
from .parsers import parse_bank_book, parse_bank_books, parse_bank_statement, file_hash, files_hash, inr, check

WINDOW_DAYS = 7
AGED_DAYS = 3
DUP_LOOKBACK_DAYS = 60        # same loan account paid twice
DUP_BENEFICIARY_DAYS = 20     # same payee + amount debited twice (monthly rent/salary stay out of range)
MIN_REASON = 15
_RUN_LOCK = threading.Lock()   # one reconciliation at a time (single-process server)


class Blocked(Exception):
    """Validation stopped the run. `checks` explains why; `ackable` lists what an override may clear."""

    def __init__(self, checks, ackable):
        super().__init__("blocked")
        self.checks = checks
        self.ackable = ackable


# ---------------------------------------------------------------------------- item helpers

def bank_item(r: dict) -> dict:
    return {
        "side": "bank", "dir": "out" if r["withdrawal"] else "in",
        "date": r["date"], "amount": r["withdrawal"] or r["deposit"],
        "name": r["name"], "ref": r["ref"], "cheque": r["cheque"], "channel": r["channel"],
        "seq": r.get("seq", ""), "branch": r.get("branch", ""), "text": r["narration"], "row_no": r["row_no"], "sl": r["sl"],
        "balance": r["balance"], "value_date": r["value_date"],
    }


def book_names(book_path, book_name):
    """Original file names of several bank books (stored joined with ' + ')."""
    if isinstance(book_path, (list, tuple)):
        n = [x.strip() for x in (book_name or "").split(" + ")]
        return n if len(n) == len(book_path) else None
    return None


def book_item(r: dict) -> dict:
    return {
        "side": "book", "dir": "out" if r["payment"] else "in",
        "date": r["date"], "amount": r["payment"] or r["receipt"],
        "name": r["name"], "ref": r["instrument"], "l4": r["l4"], "instr_no": r["instr_no"],
        "instr_date": r.get("instr_date", ""),
        "loan_ac": r["loan_ac"], "loan_type": r["loan_type"], "vno": r["vno"], "vtype": r["vtype"],
        "branch": r["branch"], "accounts": r["accounts"], "narration": r["narration"], "notes": r["notes"],
        "text": " ".join([r["narration"], r["instrument"], r["notes"], r["remarks"]]),
        "row_no": r["row_no"], "book_no": r.get("book_no", 1), "book_name": r.get("book_name", ""),
    }


def row_to_item(row) -> dict:
    d = json.loads(row["data_json"])
    d["date"] = date.fromisoformat(d["date"])
    if d.get("value_date"):
        d["value_date"] = date.fromisoformat(d["value_date"])
    d.update(key=str(row["id"]), id=row["id"], run_id=row["run_id"], match_id=row["match_id"],
             note=row["note"] if "note" in row.keys() else "")
    return d


def open_items_as_of(conn, account_id: int, run_id: int) -> list[dict]:
    """Items that were unmatched at the end of run `run_id` (reproducible for any past run)."""
    rows = conn.execute(
        """SELECT i.* FROM items i LEFT JOIN matches m ON m.id = i.match_id
           WHERE i.account_id = ? AND i.run_id <= ? AND (i.match_id IS NULL OR m.run_id > ?)
           ORDER BY i.date, i.id""", (account_id, run_id, run_id)).fetchall()
    return [row_to_item(r) for r in rows]


def cumulative_opening_diff(conn, account_id: int, upto_run: int | None = None) -> float:
    q = "SELECT COALESCE(SUM(opening_diff_added),0) s FROM runs WHERE account_id=?"
    args = [account_id]
    if upto_run is not None:
        q += " AND id <= ?"
        args.append(upto_run)
    return round(conn.execute(q, args).fetchone()["s"], 2)


LATE_METHOD = "Booked late in the ERP with the earlier date"
TIMING_CODES = ("TIMING_BANK_DEBIT", "TIMING_BANK_CREDIT", "TIMING_BOOK_PAYMENT", "TIMING_BOOK_RECEIPT")
ESCALATED_CODES = ("BANK_DEBIT_NOT_IN_BOOK", "BANK_CREDIT_NOT_IN_BOOK", "BOOK_PAYMENT_NOT_IN_BANK",
                   "BOOK_RECEIPT_NOT_IN_BANK")


def alert_for_run(conn, run_id: int, user: str) -> int | None:
    """Tell Managers and Admins what an operator's run adjusted on its own: timing differences carried to the
    next working day, entries booked late with the earlier date, and timing items that were not cleared."""
    run = conn.execute("SELECT r.*, a.nickname, a.acc_no FROM runs r JOIN accounts a ON a.id=r.account_id "
                       "WHERE r.id=?", (run_id,)).fetchone()
    flags = json.loads(run["flags_json"] or "[]")
    byk = {}
    for i in run_items(conn, run_id) + open_items_as_of(conn, run["account_id"], run_id):
        byk[i["key"]] = i

    def amt(fs):
        return sum(byk[k]["amount"] for f in fs for k in f.get("keys", []) if k in byk)

    timing = [f for f in flags if f["code"] in TIMING_CODES]
    late = [f for f in flags if f["code"] == "BOOK_LATE_POSTED"]
    esc = [f for f in flags if f["code"] in ESCALATED_CODES]
    if not (timing or late or esc):
        return None
    parts, lines = [], []
    if timing:
        parts.append(f"{len(timing)} timing difference(s) ₹{inr(amt(timing))}")
        lines.append(f"Carried to the next working day (warning): {len(timing)} line(s), ₹{inr(amt(timing))}.")
        lines += [f"  · {f['title'].split(':')[0]} — {f['detail'].split('. Treated')[0]}" for f in timing[:15]]
        if len(timing) > 15:
            lines.append(f"  · … and {len(timing) - 15} more (open the run)")
    if late:
        n = sum(len(f.get("keys", [])) for f in late)
        parts.append(f"{n} entr{'y' if n == 1 else 'ies'} booked late")
        lines += [f"Adjusted automatically: {f['title']}. {f['detail'].split('. These')[0]}." for f in late]
    if esc:
        parts.append(f"{len(esc)} not cleared by the next working day")
        lines.append(f"Not cleared by the next working day (now {', '.join(sorted({f['severity'] for f in esc}))}): "
                     f"{len(esc)} line(s), ₹{inr(amt(esc))}.")
        lines += [f"  · {f['title']} — {f['detail']}" for f in esc[:15]]
        if len(esc) > 15:
            lines.append(f"  · … and {len(esc) - 15} more (open the run)")
    sev = "critical" if any(f["severity"] == "critical" for f in esc) else ("high" if esc else "warning")
    cur = conn.execute("INSERT INTO alerts (created_at, account_id, run_id, severity, title, body, created_by) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (db.now(), run["account_id"], run_id, sev,
                        f"{run['nickname']} (…{run['acc_no'][-4:]}) · {date.fromisoformat(run['period_to']):%d-%m-%Y}: "
                        + ", ".join(parts), "\n".join(lines), user))
    return cur.lastrowid


def unread_alerts(conn, username: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM alerts a WHERE NOT EXISTS (SELECT 1 FROM alert_reads r "
                        "WHERE r.alert_id=a.id AND r.username=?)", (username,)).fetchone()[0]


def explain_late_posting(open_items: list[dict], gap: float, last_day: date) -> list[dict]:
    """Bank lines left open that the branch booked the next day but dated the earlier day.

    The ERP then shows them only in the new book's opening balance (book opening − previous book closing = gap).
    Returns the open bank lines that make up the gap exactly — preferring the previous day's own lines —
    or [] when no such set exists (then a Manager has to look at it)."""
    if abs(gap) <= TOL:
        return []
    bank = [i for i in open_items if i["side"] == "bank"]
    signed = lambda i: i["amount"] if i["dir"] == "in" else -i["amount"]   # noqa: E731
    pools = [[i for i in bank if i["date"] == last_day], bank]
    for pool in pools:
        if not pool:
            continue
        if abs(sum(signed(i) for i in pool) - gap) <= TOL:
            return pool
        same = [i for i in pool if (signed(i) > 0) == (gap > 0)]
        one = [i for i in same if abs(i["amount"] - abs(gap)) <= TOL]
        if len(one) == 1:
            return one
        sub = find_subset(abs(gap), same, max_size=12, max_items=40) if len(one) == 0 else None
        if sub:
            return sub
    return []


def net_open(items: list[dict]) -> float:
    """Effect of open items on (bank − book): + book payments − book receipts + bank credits − bank debits."""
    t = 0.0
    for i in items:
        if i["side"] == "book":
            t += i["amount"] if i["dir"] == "out" else -i["amount"]
        else:
            t += i["amount"] if i["dir"] == "in" else -i["amount"]
    return round(t, 2)


def listed_holidays(conn) -> dict:
    """Public bank holidays entered on the Holidays page: {date: name}."""
    return {date.fromisoformat(r["day"]): r["name"] for r in conn.execute("SELECT day, name FROM holidays")}


def holiday_dates(conn, account_id: int, upto_run: int) -> set:
    """Days the branches were closed, for every date reconciled up to `upto_run`.

    A day is a holiday when it is a Sunday, a bank holiday on the Holidays list, or a day on which the ERP
    bank book has no entries. Money that reaches the bank on a holiday is booked on the next working day,
    so it is held open until that day's bank book has been reconciled.
    """
    covered = set()
    for r in conn.execute("SELECT period_from, period_to FROM runs WHERE account_id=? AND id<=?",
                          (account_id, upto_run)):
        d, end = date.fromisoformat(r["period_from"]), date.fromisoformat(r["period_to"])
        while d <= end:
            covered.add(d)
            d += timedelta(days=1)
    booked = {date.fromisoformat(r["date"]) for r in conn.execute(
        "SELECT DISTINCT date FROM items WHERE account_id=? AND side='book' AND run_id<=?", (account_id, upto_run))}
    listed = listed_holidays(conn)
    return {d for d in covered if d.weekday() == 6 or d not in booked or d in listed}


def refresh_latest_runs(conn) -> int:
    """Recompute flags/BRS of each account's latest run (after the holiday list changes)."""
    n = 0
    for a in conn.execute("SELECT id FROM accounts").fetchall():
        last = latest_run(conn, a["id"])
        if last:
            refresh_run(conn, last["id"])
            n += 1
    return n


REVIEW_STATUS = {
    "ok": "Verified — OK",
    "corrected": "Corrected",
    "followup": "Needs follow-up",
}


def flag_subject(flag: dict, run_id: int) -> str:
    """Stable id for an exception: the same open items give the same id on later days,
    so a review recorded once stays attached while the item is carried forward."""
    keys = ",".join(sorted(flag.get("keys") or [], key=lambda k: int(k) if str(k).isdigit() else 0))
    code = TIMING_BASE.get(flag["code"], flag["code"])   # a timing item that escalates keeps its review
    return f"{code}|{keys or 'run' + str(run_id)}"


TIMING_BASE = {"TIMING_BANK_DEBIT": "BANK_DEBIT_NOT_IN_BOOK", "TIMING_BANK_CREDIT": "BANK_CREDIT_NOT_IN_BOOK",
               "TIMING_BOOK_PAYMENT": "BOOK_PAYMENT_NOT_IN_BANK", "TIMING_BOOK_RECEIPT": "BOOK_RECEIPT_NOT_IN_BANK"}


def latest_reviews(conn, account_id: int) -> dict:
    """{subject: latest review row} for an account."""
    out = {}
    for r in conn.execute("SELECT * FROM reviews WHERE account_id=? ORDER BY id", (account_id,)):
        out[r["subject"]] = dict(r)
    return out


def attach_reviews(conn, run, flags: list[dict]) -> list[dict]:
    revs = latest_reviews(conn, run["account_id"])
    for f in flags:
        f["subject"] = flag_subject(f, run["id"])
        rv = revs.get(f["subject"])
        if rv:
            rv = dict(rv, label=REVIEW_STATUS.get(rv["status"], rv["status"]))
        f["review"] = rv
    return flags


def save_review(conn, run_id: int, subject: str, status: str, remark: str, user: str) -> None:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Run not found.")
    if status not in REVIEW_STATUS:
        raise ValueError("Pick a review status.")
    if status != "ok" and len(remark.strip()) < 5:
        raise ValueError("Write what was corrected or what follow-up is needed.")
    flags = json.loads(run["flags_json"] or "[]")
    if subject not in {flag_subject(f, run_id) for f in flags}:
        raise ValueError("That exception is no longer on this run.")
    conn.execute("INSERT INTO reviews(account_id, run_id, subject, status, remark, username, at) VALUES (?,?,?,?,?,?,?)",
                 (run["account_id"], run_id, subject, status, remark.strip()[:500], user, db.now()))
    db.audit(conn, user, "exception_reviewed",
             f"Run #{run_id} · {subject.split('|')[0]} · {REVIEW_STATUS[status]}" + (f" · {remark.strip()}" if remark.strip() else ""))


def latest_run(conn, account_id: int):
    return conn.execute("SELECT * FROM runs WHERE account_id=? ORDER BY period_to DESC, id DESC LIMIT 1",
                        (account_id,)).fetchone()


# ---------------------------------------------------------------------------- main entry

def reconcile(conn, account_id: int, bank_path: str, bank_name: str, book_path: str, book_name: str,
              user: str, acknowledged: bool = False, ack_reason: str = "", backdated_path: str | None = None,
              backdated_name: str = "", confirmed: bool | None = None) -> int:
    with _RUN_LOCK:
        return _reconcile(conn, account_id, bank_path, bank_name, book_path, book_name, user, acknowledged, ack_reason,
                          backdated_path, backdated_name, confirmed)


def _book_key(it: dict) -> tuple:
    return (str(it.get("vno") or ""), (it.get("branch") or "").strip().upper(), it["dir"], round(it["amount"] * 100))


def find_backdated(conn, account_id: int, last, fresh_path: str, book: dict) -> dict:
    """Compare a fresh ERP export of days already reconciled with what the app stored for those days.

    Vouchers in the fresh export but not stored were posted (or changed) after the day was reconciled;
    stored vouchers missing from it were deleted or changed. If they account for the whole gap between the
    previous book closing and today's book opening, the gap is explained voucher by voucher."""
    fresh = parse_bank_books(fresh_path)  # one file, or several books of the same ledger
    out = {"explains": False, "added": [], "removed_open": [], "removed_matched": [], "net": 0.0, "message": ""}
    if not fresh.get("ok"):
        out["message"] = ("The re-downloaded bank book could not be read: "
                          + "; ".join(c["message"] for c in fresh["checks"] if c["level"] == "error"))
        return out
    f_from, f_to = fresh["period_from"], fresh["period_to"]
    last_to = date.fromisoformat(last["period_to"])
    first = conn.execute("SELECT MIN(period_from) d FROM runs WHERE account_id=?", (account_id,)).fetchone()["d"]
    if f_to != last_to:
        out["message"] = (f"The re-downloaded bank book must end on {last_to:%d-%m-%Y}, the last reconciled day "
                          f"(it covers {f_from:%d-%m-%Y} to {f_to:%d-%m-%Y}).")
        return out
    if first and f_from < date.fromisoformat(first):
        f_from = date.fromisoformat(first)
    deleted_mids = {m["id"] for m in conn.execute(
        "SELECT id FROM matches WHERE account_id=? AND method LIKE 'Voucher deleted%'", (account_id,))}
    stored = [row_to_item(r) for r in conn.execute(
        "SELECT * FROM items WHERE account_id=? AND side='book' AND date>=? AND date<=? ORDER BY id",
        (account_id, f_from.isoformat(), f_to.isoformat()))]
    stored = [i for i in stored if i.get("match_id") not in deleted_mids]
    fresh_items = [book_item(r) for r in fresh["rows"] if f_from <= r["date"] <= f_to]
    pool: dict[tuple, list] = defaultdict(list)
    for it in stored:
        pool[_book_key(it)].append(it)
    added = []
    for it in fresh_items:
        k = _book_key(it)
        if pool.get(k):
            pool[k].pop(0)
        else:
            added.append(it)
    removed = [it for lst in pool.values() for it in lst]
    sign = lambda it: it["amount"] if it["dir"] == "in" else -it["amount"]
    net = round(sum(sign(i) for i in added) - sum(sign(i) for i in removed), 2)
    removed_open = [i for i in removed if not i.get("match_id")]
    removed_matched = [i for i in removed if i.get("match_id")]
    out.update(added=added, removed_open=removed_open, removed_matched=removed_matched, net=net)
    desc = lambda i: f"Vchr {i.get('vno')} {i['date']:%d-%m-%Y} {i.get('name') or i.get('narration', '')[:30]} ₹{inr(i['amount'])}"
    parts = []
    if added:
        parts.append(f"{len(added)} voucher(s) posted after reconciliation: " + "; ".join(desc(i) for i in added[:8])
                     + (f" and {len(added) - 8} more" if len(added) > 8 else ""))
    if removed:
        parts.append(f"{len(removed)} voucher(s) deleted or changed since: " + "; ".join(desc(i) for i in removed[:8]))
    found = (" Found " + ". ".join(parts) + f". Net effect ₹{inr(net)}.") if parts else " No voucher differs."
    gap = round(book["opening"] - last["book_close"], 2)
    if removed_matched:
        out["message"] = ("Some vouchers that were already matched have since been deleted or changed in the ERP, so "
                          "the earlier reconciliation itself is now wrong. An Admin should revoke from "
                          f"{min(i['date'] for i in removed_matched):%d-%m-%Y} and reconcile again." + found)
    elif abs(fresh["closing"] - book["opening"]) > TOL:
        out["message"] = (f"The re-downloaded bank book closes at ₹{inr(fresh['closing'])} on {f_to:%d-%m-%Y}, but "
                          f"today's bank book opens at ₹{inr(book['opening'])}. Download both again at the same time." + found)
    elif abs(net - gap) > TOL:
        out["message"] = (f"The differences found (₹{inr(net)}) do not fully explain the gap of ₹{inr(gap)}. "
                          f"There may be back-dated entries before {f_from:%d-%m-%Y}: download the bank book from an "
                          "earlier date." + found)
    else:
        out["explains"] = True
        out["message"] = (f"Book opening ₹{inr(book['opening'])} = previous closing ₹{inr(last['book_close'])} + "
                          f"back-dated changes ₹{inr(net)}, found in the re-downloaded bank book "
                          f"{f_from:%d-%m-%Y} to {f_to:%d-%m-%Y}." + found
                          + " They are added to this reconciliation and flagged.")
    return out


def _reconcile(conn, account_id, bank_path, bank_name, book_path, book_name, user, acknowledged, ack_reason,
               backdated_path=None, backdated_name="", confirmed=None) -> int:
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        raise Blocked([check("error", "ACCOUNT", "Unknown account.")], [])

    bank = parse_bank_statement(bank_path)
    book = parse_bank_books(book_path, book_names(book_path, book_name))
    checks = [dict(c, source="Bank statement") for c in bank["checks"]] + \
             [dict(c, source="Bank book") for c in book["checks"]]
    if not bank.get("ok") or not book.get("ok"):
        raise Blocked(checks, [])

    ackable: list[dict] = []
    cross: list[dict] = []

    def add(level, code, msg, can_ack=False):
        c = dict(check(level, code, msg), source="Cross-check")
        cross.append(c)
        if can_ack:
            ackable.append(c)

    # --- the two files must be for the same account and period
    if bank["account_no"] and not bank["account_no"].rstrip("*Xx").endswith(acc["acc_no"][-4:]):
        add("error", "ACCOUNT_MISMATCH",
            f"Statement shows account {bank['account_no']}, but you selected …{acc['acc_no'][-4:]}.")
    p_from, p_to = book["period_from"], book["period_to"]
    # SIB (and ICICI) often add a line just after midnight, dated the next day, to the end of a day's
    # statement. It belongs to the next day: leave it out and close the day on the balance before it.
    deferred = [r for r in bank["rows"] if r["date"] > p_to]
    if deferred and bank["rows"] and bank["rows"][-len(deferred):] == deferred:
        bank["rows"] = bank["rows"][:-len(deferred)]
        bank["closing"] = bank["rows"][-1]["balance"] if bank["rows"] else bank["opening"]
        add("ok", "BANK_NEXT_DAY",
            f"{len(deferred)} line(s) dated after {p_to:%d-%m-%Y} at the end of the statement were left out "
            f"(₹{inr(sum(r['deposit'] - r['withdrawal'] for r in deferred))}, e.g. "
            f"{deferred[0]['date']:%d-%m-%Y} {deferred[0]['narration'][:50]}). They belong to the next day's "
            f"reconciliation. Bank closing for this day taken as ₹{inr(bank['closing'])}.")
    else:
        deferred = []
    outside = [r for r in bank["rows"] if r["date"] < p_from or r["date"] > p_to]
    if outside:
        add("error", "PERIOD_MISMATCH",
            f"Bank statement has {len(outside)} transaction(s) outside the bank book period "
            f"{p_from:%d-%m-%Y} to {p_to:%d-%m-%Y} (e.g. {outside[0]['date']:%d-%m-%Y}). "
            "Download both reports for the same date(s).")
    else:
        add("ok", "PERIOD", f"Both files cover {p_from:%d-%m-%Y}" +
            (f" to {p_to:%d-%m-%Y}" if p_to != p_from else "") + ".")

    # --- same file must not be reconciled twice
    bh, kh = file_hash(bank_path), files_hash(book_path)
    singles = [] if isinstance(book_path, (str,)) else [(file_hash(p), "bank book") for p in book_path if p]
    for h, label in ((bh, "bank statement"), (kh, "bank book"), *singles):
        prev = conn.execute("SELECT r.id, r.period_to, a.nickname FROM runs r JOIN accounts a ON a.id=r.account_id "
                            "WHERE r.bank_hash=? OR r.book_hash=?", (h, h)).fetchone()
        if prev:
            add("error", "DUPLICATE_FILE",
                f"This exact {label} file was already used in run #{prev['id']} "
                f"({prev['nickname']}, {prev['period_to']}).")
    if len({h for h, _ in singles}) < len(singles):
        add("error", "SAME_BOOK_TWICE", "The same bank book file was selected twice.")
    if bh == kh or any(bh == h for h, _ in singles):
        add("error", "SAME_FILE", "The same file was uploaded as both bank statement and bank book.")

    # --- sequence and continuity with the previous run
    last = latest_run(conn, account_id)
    carried = []
    backdated_new: list[dict] = []
    backdated_gone: list[dict] = []
    late_posted: list[dict] = []
    if confirmed:
        add("ok", "CONFIRM_BACKDATED", f"{user} confirmed that all back-dated entries are posted and updated, and that "
                                       "there will be no more back-dated entries after downloading the report.")
    elif confirmed is False:
        add("error", "CONFIRM_BACKDATED", "Confirm that all back-dated entries are posted before reconciling.")
    if last:
        last_to = date.fromisoformat(last["period_to"])
        if p_from <= last_to:
            add("error", "ALREADY_DONE",
                f"This account is already reconciled up to {last_to:%d-%m-%Y}. "
                "To redo a date, undo the later run(s) first (History → Undo).")
            existing = conn.execute("SELECT id FROM runs WHERE account_id=? AND period_from<=? AND period_to>=? "
                                    "ORDER BY id DESC LIMIT 1", (account_id, p_to.isoformat(), p_from.isoformat())).fetchone()
            if existing:
                cross[-1]["run_id"] = existing["id"]
            raise Blocked(checks + cross, [])
        if p_from > last_to + timedelta(days=1):
            add("warning", "DATE_GAP",
                f"Previous reconciliation ended {last_to:%d-%m-%Y}; this one starts {p_from:%d-%m-%Y}. "
                "Days in between are not covered (fine for Sundays/holidays only if there were no transactions).",
                can_ack=True)
        prev_def = json.loads(last["deferred_json"] or "[]") if "deferred_json" in last.keys() else []
        for r in prev_def:
            r["date"] = date.fromisoformat(r["date"])
            r["value_date"] = date.fromisoformat(r["value_date"]) if r.get("value_date") else r["date"]
        if prev_def:
            net_def = round(sum(r["deposit"] - r["withdrawal"] for r in prev_def), 2)
            if (abs(last["bank_close"] - bank["opening"]) > TOL
                    and abs(last["bank_close"] + net_def - bank["opening"]) <= TOL):
                # today's statement starts after those lines: bring them in from yesterday's file
                for r in prev_def:
                    r["carried_from_prev_statement"] = True
                bank["rows"] = prev_def + bank["rows"]
                bank["opening"] = last["bank_close"]
                add("ok", "BANK_NEXT_DAY_IN",
                    f"{len(prev_def)} late-night line(s) from the previous statement (₹{inr(net_def)}) are not in "
                    "today's statement, so they were brought in from yesterday's file.")
            elif abs(last["bank_close"] - bank["opening"]) <= TOL:
                missing = [r for r in prev_def if not any(
                    abs(x["deposit"] - r["deposit"]) <= TOL and abs(x["withdrawal"] - r["withdrawal"]) <= TOL
                    and (x["ref"] == r["ref"] or x["narration"] == r["narration"]) for x in bank["rows"])]
                if missing:
                    add("warning", "BANK_NEXT_DAY_MISSING",
                        f"{len(missing)} late-night line(s) left out of the previous day are not in today's statement: "
                        + "; ".join(f"{r['narration'][:40]} ₹{inr(r['deposit'] or r['withdrawal'])}" for r in missing[:5])
                        + ". Check with the bank whether they were reversed.")
                else:
                    add("ok", "BANK_NEXT_DAY_IN",
                        f"{len(prev_def)} late-night line(s) left out of the previous day appear in today's statement.")
        if abs(last["bank_close"] - bank["opening"]) > TOL:
            add("error", "BANK_CONTINUITY",
                f"Bank opening ₹{inr(bank['opening'])} ≠ previous bank closing ₹{inr(last['bank_close'])} "
                f"(difference ₹{inr(bank['opening'] - last['bank_close'])}). Either a day with transactions has "
                "not been reconciled, the statement is incomplete, or it is for a different account. "
                "This cannot be overridden — reconcile the missing day(s) first.")
        else:
            add("ok", "BANK_CONTINUITY", f"Bank opening ₹{inr(bank['opening'])} = previous closing.")
        if abs(last["book_close"] - book["opening"]) > TOL and backdated_path:
            bd = find_backdated(conn, account_id, last, backdated_path, book)
            if bd["explains"]:
                add("ok", "BOOK_CONTINUITY", bd["message"])
                backdated_new, backdated_gone = bd["added"], bd["removed_open"]
            else:
                add("error" if not acknowledged else "warning", "BOOK_CONTINUITY",
                    f"Book opening ₹{inr(book['opening'])} ≠ previous book closing ₹{inr(last['book_close'])} "
                    f"(difference ₹{inr(book['opening'] - last['book_close'])}). " + bd["message"], can_ack=True)
        elif abs(last["book_close"] - book["opening"]) > TOL and (late_posted := explain_late_posting(
                open_items_as_of(conn, account_id, last["id"]), round(book["opening"] - last["book_close"], 2),
                date.fromisoformat(last["period_to"]))):
            add("warning", "BOOK_LATE_POSTED",
                f"Book opening ₹{inr(book['opening'])} ≠ previous book closing ₹{inr(last['book_close'])} "
                f"(difference ₹{inr(book['opening'] - last['book_close'])}). This equals {len(late_posted)} bank "
                f"line(s) left open on {last['period_to']}, so they were booked in the ERP the next day with the "
                "earlier date: " + "; ".join(f"{i['date']:%d-%m} {i['name'] or i.get('text', '')[:30]} "
                                             f"{'+' if i['dir'] == 'in' else '−'}₹{inr(i['amount'])}" for i in late_posted[:8])
                + ". Adjusted automatically as booked late; Managers and Admins are alerted.")
        elif abs(last["book_close"] - book["opening"]) > TOL:
            add("error" if not acknowledged else "warning", "BOOK_CONTINUITY",
                f"Book opening ₹{inr(book['opening'])} ≠ previous book closing ₹{inr(last['book_close'])} "
                f"(difference ₹{inr(book['opening'] - last['book_close'])}). Usually entries were posted, changed or "
                f"deleted in the ERP for a date already reconciled (up to {last['period_to']}). Use 'Find the "
                "back-dated entries' below with a fresh bank book export of the earlier day(s), or acknowledge to carry "
                "the difference as an unexplained item.",
                can_ack=True)
        else:
            add("ok", "BOOK_CONTINUITY", f"Book opening ₹{inr(book['opening'])} = previous closing.")
        carried = open_items_as_of(conn, account_id, last["id"])
        gone_ids = {i["id"] for i in backdated_gone} | {i["id"] for i in late_posted}
        carried = [c for c in carried if c["id"] not in gone_ids]

    if not last:
        kinds = {r["channel"] for r in bank["rows"] if r["withdrawal"]}
        if acc["kind"] == "collection" and "RAP" in kinds:
            add("error", "ACCOUNT_KIND",
                "This statement has RAP (bank API) disbursals, which only the main account should have. "
                "Check you picked the right account.")
        add("warning", "FIRST_RUN",
            f"First reconciliation for {acc['nickname']} (…{acc['acc_no'][-4:]}). The statement has no account "
            f"number printed, so confirm it is for this account: bank opening balance ₹{inr(bank['opening'])} "
            f"on {bank['period_from']:%d-%m-%Y}. Every later day must continue from this.", can_ack=True)

    # --- the identity that must hold at the opening of the period
    prior_diff = cumulative_opening_diff(conn, account_id)
    for it in backdated_new:
        it["side"] = "book"
    unexplained = round(bank["opening"] - book["opening"] - net_open(carried) - net_open(backdated_new) - prior_diff, 2)
    if abs(unexplained) > TOL:
        if not last:
            add("warning", "OPENING_DIFF",
                f"First reconciliation for this account: bank opening ₹{inr(bank['opening'])} and book opening "
                f"₹{inr(book['opening'])} differ by ₹{inr(unexplained)}. It will be carried in the BRS as "
                "'Opening difference (not itemised)' and flagged CRITICAL on every run until explained.", can_ack=True)
        else:
            add("error" if not acknowledged else "warning", "OPENING_IDENTITY",
                f"Opening balances do not agree with the open items carried forward: unexplained ₹{inr(unexplained)}. "
                "If you proceed, this amount is carried as an unexplained difference and flagged CRITICAL on every "
                "run until it is explained.", can_ack=True)
    else:
        add("ok", "OPENING_IDENTITY",
            "Opening bank − opening book = open items carried forward" +
            (f" ({len(carried)} item(s))." if carried else " (no open items)."))

    all_checks = checks + cross
    hard_errors = [c for c in cross if c["level"] == "error" and c not in ackable]
    if hard_errors:
        raise Blocked(all_checks, [])
    if ackable and not acknowledged:
        raise Blocked(all_checks, ackable)
    if ackable and acknowledged and len(ack_reason.strip()) < MIN_REASON:
        raise Blocked(all_checks + [dict(check("error", "ACK_REASON",
                      f"Write a proper reason (at least {MIN_REASON} characters) to proceed past these warnings."),
                      source="Cross-check")], ackable)
    opening_added = unexplained if abs(unexplained) > TOL else 0.0

    # --- store (re-checked inside the write lock so a double-submit cannot create two runs)
    with db.tx(conn):
        now_last = latest_run(conn, account_id)
        if (now_last["id"] if now_last else None) != (last["id"] if last else None) or conn.execute(
                "SELECT 1 FROM runs WHERE bank_hash IN (?,?) OR book_hash IN (?,?)", (bh, kh, bh, kh)).fetchone():
            raise Blocked(all_checks + [dict(check("error", "CONCURRENT",
                          "Another reconciliation for this account was saved a moment ago. Refresh and check History."),
                          source="Cross-check")], [])
        cur = conn.execute(
            """INSERT INTO runs (account_id, period_from, period_to, created_at, created_by, bank_file, bank_hash,
               book_file, book_hash, bank_open, bank_close, book_open, book_close, opening_diff_added, ack_reason,
               checks_json, deferred_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, p_from.isoformat(), p_to.isoformat(), db.now(), user, bank_name, bh, book_name, kh,
             bank["opening"], bank["closing"], book["opening"], book["closing"], opening_added,
             ack_reason.strip(), db.dumps(all_checks), db.dumps(deferred) if deferred else ""))
        run_id = cur.lastrowid
        new_items = []
        for r in bank["rows"]:
            it = bank_item(r)
            cur = conn.execute("INSERT INTO items (run_id, account_id, side, dir, date, amount, name, ref, data_json) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (run_id, account_id, "bank", it["dir"], it["date"].isoformat(), it["amount"],
                                it["name"], it["ref"], db.dumps(it)))
            new_items.append(dict(it, key=str(cur.lastrowid), id=cur.lastrowid, run_id=run_id))
        for r in book["rows"]:
            it = book_item(r)
            cur = conn.execute("INSERT INTO items (run_id, account_id, side, dir, date, amount, name, ref, data_json) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (run_id, account_id, "book", it["dir"], it["date"].isoformat(), it["amount"],
                                it["name"], it["ref"], db.dumps(it)))
            new_items.append(dict(it, key=str(cur.lastrowid), id=cur.lastrowid, run_id=run_id))
        for it in backdated_new:
            it = dict(it, backdated=True, posted_after=last["period_to"] if last else "")
            it.pop("side", None)
            it["side"] = "book"
            cur = conn.execute("INSERT INTO items (run_id, account_id, side, dir, date, amount, name, ref, data_json) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (run_id, account_id, "book", it["dir"], it["date"].isoformat(), it["amount"],
                                it["name"], it["ref"], db.dumps(it)))
            new_items.append(dict(it, key=str(cur.lastrowid), id=cur.lastrowid, run_id=run_id))
        if backdated_gone:
            cur = conn.execute(
                "INSERT INTO matches (run_id, account_id, method, confidence, note, manual, created_at, created_by) "
                "VALUES (?,?,?,?,?,0,?,?)",
                (run_id, account_id, "Voucher deleted or changed in the ERP after reconciliation", "review",
                 f"{len(backdated_gone)} open voucher(s) no longer in the ERP export: "
                 + "; ".join(f"Vchr {i.get('vno')} ₹{inr(i['amount'])}" for i in backdated_gone[:10]),
                 db.now(), "system"))
            conn.execute(f"UPDATE items SET match_id=? WHERE id IN ({','.join('?' * len(backdated_gone))})",
                         (cur.lastrowid, *[i["id"] for i in backdated_gone]))
        if late_posted:
            cur = conn.execute(
                "INSERT INTO matches (run_id, account_id, method, confidence, note, manual, created_at, created_by) "
                "VALUES (?,?,?,?,?,0,?,?)",
                (run_id, account_id, LATE_METHOD, "review",
                 f"Bank line(s) of {last['period_to']} booked in the ERP the next day with the earlier date "
                 f"(they make up the change in the book's opening balance). Adjusted automatically for {user}.",
                 db.now(), "system"))
            conn.execute(f"UPDATE items SET match_id=? WHERE id IN ({','.join('?' * len(late_posted))})",
                         (cur.lastrowid, *[i["id"] for i in late_posted]))
            db.audit(conn, user, "late_posted_adjusted",
                     f"Run #{run_id}: {len(late_posted)} bank line(s) of {last['period_to']} booked late, "
                     f"₹{inr(sum(i['amount'] for i in late_posted))}")
        if backdated_path:
            db.audit(conn, user, "backdated_check",
                     f"Run #{run_id}: re-downloaded bank book {backdated_name} · {len(backdated_new)} added, "
                     f"{len(backdated_gone)} deleted/changed")
        for c in carried:
            c["carried"] = True

        bank_items = [i for i in carried + new_items if i["side"] == "bank"]
        book_items = [i for i in carried + new_items if i["side"] == "book"]
        branch_names = [n for r in conn.execute("SELECT branch, nickname FROM accounts")
                        for n in (r["branch"], r["nickname"]) if n]
        branch_names += [b.get("branch", "") for b in book_items if b.get("branch")]
        matches = Matcher(bank_items, book_items, window_days=WINDOW_DAYS,
                          holidays=holiday_dates(conn, account_id, run_id), branches=branch_names).run()
        for m in matches:
            carried_keys = [k for k in (*m["bank"], *m["book"]) if any(c["key"] == k for c in carried)]
            note = m["note"]
            if carried_keys:
                note = (note + " · " if note else "") + "clears item(s) carried from an earlier day"
            cur = conn.execute("INSERT INTO matches (run_id, account_id, method, confidence, note, manual, created_at, "
                               "created_by) VALUES (?,?,?,?,?,0,?,?)",
                               (run_id, account_id, m["method"], m["confidence"], note, db.now(), "system"))
            mid = cur.lastrowid
            keys = [int(k) for k in (*m["bank"], *m["book"])]
            conn.execute(f"UPDATE items SET match_id=? WHERE id IN ({','.join('?' * len(keys))})", (mid, *keys))
        refresh_run(conn, run_id)
        alert_for_run(conn, run_id, user)
        db.audit(conn, user, "reconcile",
                 f"Run #{run_id} · {acc['nickname']} · {p_from}..{p_to} · bank {bank_name} · book {book_name}"
                 + (f" · acknowledged: {ack_reason.strip()}" if ackable else ""))
    return run_id


# ---------------------------------------------------------------------------- derived data

def run_items(conn, run_id: int):
    """Everything relevant to a run: its own items plus earlier items it matched."""
    rows = conn.execute(
        """SELECT i.* FROM items i LEFT JOIN matches m ON m.id=i.match_id
           WHERE i.run_id=? OR m.run_id=? ORDER BY i.side, i.date, i.id""", (run_id, run_id)).fetchall()
    return [row_to_item(r) for r in rows]


def run_matches(conn, run_id: int):
    ms = [dict(r) for r in conn.execute("SELECT * FROM matches WHERE run_id=? ORDER BY id", (run_id,))]
    return ms


def history_flags(conn, run, own_items: list[dict]) -> list[dict]:
    """Double-payment checks against earlier days (the matcher only sees today + open items)."""
    flags = []
    since = (date.fromisoformat(run["period_from"]) - timedelta(days=DUP_LOOKBACK_DAYS)).isoformat()
    prior = [row_to_item(r) for r in conn.execute(
        "SELECT * FROM items WHERE account_id=? AND run_id<>? AND dir='out' AND date>=?",
        (run["account_id"], run["id"], since))]
    by_loan = defaultdict(list)
    by_ben = defaultdict(list)
    from .matcher import clean_name
    for p in prior:
        if p["side"] == "book" and p.get("loan_ac"):
            by_loan[p["loan_ac"]].append(p)
        if p["side"] == "bank" and p.get("name"):
            by_ben[(clean_name(p["name"]), round(p["amount"], 2))].append(p)
    for it in own_items:
        if it["dir"] != "out":
            continue
        if it["side"] == "book" and it.get("loan_ac") and by_loan.get(it["loan_ac"]):
            prev = by_loan[it["loan_ac"]]
            flags.append({"severity": "high", "code": "DUP_LOAN_PAYMENT_HISTORY",
                          "title": "Loan account already paid on an earlier day",
                          "detail": f"Loan A/c {it['loan_ac']} ({it['name']}) · today Vchr {it.get('vno')} ₹{inr(it['amount'])}"
                                    " · earlier: " + ", ".join(f"{p['date']:%d-%m-%Y} Vchr {p.get('vno')} ₹{inr(p['amount'])}"
                                                               for p in prev[:5]),
                          "keys": [it["key"]] + [p["key"] for p in prev[:5]]})
        if it["side"] == "bank" and it.get("name"):
            prev = [p for p in by_ben.get((clean_name(it["name"]), round(it["amount"], 2)), [])
                    if (it["date"] - p["date"]).days <= DUP_BENEFICIARY_DAYS]
            if prev:
                flags.append({"severity": "high", "code": "DUP_BANK_PAYMENT_HISTORY",
                              "title": "Same beneficiary and amount already debited on an earlier day",
                              "detail": f"{it['name']} ₹{inr(it['amount'])} · earlier: "
                                        + ", ".join(f"{p['date']:%d-%m-%Y}" for p in prev[:5]),
                              "keys": [it["key"]] + [p["key"] for p in prev[:5]]})
    return flags


def refresh_run(conn, run_id: int) -> None:
    """(Re)compute flags, BRS and summary for a run from the database state."""
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (run["account_id"],)).fetchone()
    items = run_items(conn, run_id)
    open_now = open_items_as_of(conn, run["account_id"], run_id)
    matches = run_matches(conn, run_id)
    by_match: dict[int, dict] = {}
    for i in items:
        if i.get("match_id") and any(m["id"] == i["match_id"] for m in matches):
            mm = by_match.setdefault(i["match_id"], {"bank": [], "book": []})
            mm[i["side"]].append(i["key"])
    mlist = []
    for m in matches:
        mm = by_match.get(m["id"], {"bank": [], "book": []})
        mlist.append({"bank": mm["bank"], "book": mm["book"], "method": m["method"],
                      "confidence": m["confidence"], "note": m["note"]})
    # flags consider this run's items and everything still open
    keys = {i["key"] for i in items}
    universe = items + [o for o in open_now if o["key"] not in keys]
    for u in universe:
        u["carried"] = u["run_id"] != run_id
    bank_u = [i for i in universe if i["side"] == "bank"]
    book_u = [i for i in universe if i["side"] == "book"]
    as_of = date.fromisoformat(run["period_to"])
    holidays = holiday_dates(conn, run["account_id"], run_id)
    flags = build_flags(bank_u, book_u, mlist, acc["kind"], as_of, aged_days=AGED_DAYS, holidays=holidays,
                        holiday_names=listed_holidays(conn))
    flags = history_flags(conn, run, [i for i in items if i["run_id"] == run_id]) + flags
    brs = compute_brs(run["book_close"], run["bank_close"],
                      [o for o in open_now if o["side"] == "bank"],
                      [o for o in open_now if o["side"] == "book"],
                      cumulative_opening_diff(conn, run["account_id"], run_id), holidays=holidays)
    if abs(brs["opening_diff"]) > TOL:
        flags.insert(0, {"severity": "critical", "code": "UNEXPLAINED_DIFFERENCE",
                         "title": f"Unexplained difference of ₹{inr(brs['opening_diff'])} carried in the BRS",
                         "detail": "Accepted with an acknowledgement on an earlier run and not itemised. The BRS only "
                                   "balances because of it. Find the cause (missed entry, back-dated voucher) and correct it.",
                         "keys": []})
    for m in matches:
        if m["method"] == LATE_METHOD:
            its = [row_to_item(r) for r in conn.execute("SELECT * FROM items WHERE match_id=?", (m["id"],))]
            flags.append({"severity": "medium", "code": "BOOK_LATE_POSTED",
                          "title": f"{len(its)} entr{'y' if len(its) == 1 else 'ies'} booked late in the ERP with the "
                                   "previous day's date (adjusted automatically)",
                          "detail": "; ".join(f"{i['date']:%d-%m-%Y} {i['name'] or i.get('text', '')[:40]} "
                                              f"{'credit' if i['dir'] == 'in' else 'debit'} ₹{inr(i['amount'])}"
                                              for i in its[:10])
                                    + ". These were open in the bank yesterday and the ERP now has them dated "
                                      "yesterday. Branches should post on the day; check who posted them.",
                          "keys": [i["key"] for i in its]})
    from .matcher import SEV_ORDER
    flags.sort(key=lambda x: SEV_ORDER[x["severity"]])
    own = [i for i in items if i["run_id"] == run_id]

    def stat(side, direction):
        xs = [i for i in own if i["side"] == side and i["dir"] == direction]
        mt = [i for i in xs if i.get("match_id")]
        return {"count": len(xs), "amount": round(sum(i["amount"] for i in xs), 2),
                "matched": len(mt), "matched_amount": round(sum(i["amount"] for i in mt), 2)}

    summary = {
        "bank_out": stat("bank", "out"), "bank_in": stat("bank", "in"),
        "book_out": stat("book", "out"), "book_in": stat("book", "in"),
        "matches": len(matches),
        "review_matches": sum(1 for m in matches if m["confidence"] == "review"),
        "manual_matches": sum(1 for m in matches if m["manual"]),
        "open_items": len(open_now),
        "carried_cleared": sum(1 for i in items if i["run_id"] != run_id),
        "severity": {s: sum(1 for f in flags if f["severity"] == s) for s in ("critical", "high", "medium", "info")},
        # the run's own dates that were holidays (Sunday or no bank book entries)
        "holidays": sorted(d.isoformat() for d in holidays
                           if date.fromisoformat(run["period_from"]) <= d <= as_of),
    }
    conn.execute("UPDATE runs SET flags_json=?, brs_json=?, summary_json=? WHERE id=?",
                 (db.dumps(flags), db.dumps(brs), db.dumps(summary), run_id))


# ---------------------------------------------------------------------------- manual actions

def manual_match(conn, account_id: int, item_ids: list[int], remark: str, user: str) -> int:
    if not remark.strip():
        raise ValueError("A remark is required for a manual match.")
    last = latest_run(conn, account_id)
    if not last:
        raise ValueError("No reconciliation exists for this account.")
    rows = conn.execute(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(item_ids))}) AND account_id=?",
                        (*item_ids, account_id)).fetchall()
    if len(rows) != len(set(item_ids)):
        raise ValueError("Some selected items were not found for this account.")
    if any(r["match_id"] for r in rows):
        raise ValueError("Some selected items are already matched. Refresh the page.")
    bank = [r for r in rows if r["side"] == "bank"]
    book = [r for r in rows if r["side"] == "book"]
    if not bank or not book:
        raise ValueError("Select at least one bank line and one book entry.")
    dirs = {r["dir"] for r in rows}
    if len(dirs) != 1:
        raise ValueError("Bank debits can only match book payments, and bank credits only book receipts.")
    tb, tk = round(sum(r["amount"] for r in bank), 2), round(sum(r["amount"] for r in book), 2)
    if abs(tb - tk) > TOL:
        raise ValueError(f"Amounts differ: bank ₹{inr(tb)} vs book ₹{inr(tk)}. A match must be exact.")
    with db.tx(conn):
        cur = conn.execute("INSERT INTO matches (run_id, account_id, method, confidence, note, manual, created_at, "
                           "created_by) VALUES (?,?,?,?,?,1,?,?)",
                           (last["id"], account_id, "Manual match", "manual", remark.strip(), db.now(), user))
        mid = cur.lastrowid
        conn.execute(f"UPDATE items SET match_id=? WHERE id IN ({','.join('?' * len(item_ids))})", (mid, *item_ids))
        refresh_run(conn, last["id"])
        db.audit(conn, user, "manual_match", f"Match #{mid} · {len(bank)} bank + {len(book)} book · ₹{inr(tb)} · {remark.strip()}")
    return mid


def unmatch(conn, match_id: int, user: str, reason: str) -> None:
    m = conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
    if not m:
        raise ValueError("Match not found.")
    last = latest_run(conn, m["account_id"])
    if m["run_id"] != last["id"]:
        raise ValueError("Only matches in the latest reconciliation can be undone.")
    if not reason.strip():
        raise ValueError("A reason is required.")
    with db.tx(conn):
        for r in conn.execute("SELECT id FROM items WHERE match_id=?", (match_id,)).fetchall():
            conn.execute("INSERT OR IGNORE INTO no_auto(item_id, at, username) VALUES (?,?,?)", (r["id"], db.now(), user))
        conn.execute("UPDATE items SET match_id=NULL WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM matches WHERE id=?", (match_id,))
        refresh_run(conn, last["id"])
        db.audit(conn, user, "unmatch", f"Match #{match_id} ({m['method']}) removed · {reason.strip()}")


def add_note(conn, item_id: int, note: str, user: str) -> None:
    with db.tx(conn):
        conn.execute("UPDATE items SET note=? WHERE id=?", (note.strip(), item_id))
        db.audit(conn, user, "note", f"Item #{item_id}: {note.strip()}")


def _delete_run(conn, run_id: int) -> None:
    """Remove a run, its matches and items; items it cleared from earlier days become open again."""
    conn.execute("UPDATE items SET match_id=NULL WHERE match_id IN (SELECT id FROM matches WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM matches WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM items WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
    conn.execute("DELETE FROM alert_reads WHERE alert_id IN (SELECT id FROM alerts WHERE run_id=?)", (run_id,))
    conn.execute("DELETE FROM alerts WHERE run_id=?", (run_id,))


RESET_TABLES = ("alert_reads", "alerts", "reviews", "no_auto", "revoked_runs", "items", "matches", "runs")


def reset_all(conn, user: str, backup_dir) -> dict:
    """Clear every reconciliation (runs, matched/open items, reviews, alerts) so the team can start again from a
    date of their choice. Users, bank accounts, holidays, settings and the audit log stay. A full copy of the
    database is written to `backup_dir` first, so nothing is lost."""
    from pathlib import Path
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = backup_dir / f"before-reset_{stamp}.sqlite3"
    dst = sqlite3.connect(target)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in RESET_TABLES}
    with db.tx(conn):
        for t in RESET_TABLES:
            conn.execute(f"DELETE FROM {t}")
        db.audit(conn, user, "reset_all",
                 f"All reconciliations cleared ({counts['runs']} run(s), {counts['items']} line(s), "
                 f"{counts['reviews']} review(s), {counts['alerts']} alert(s)). Backup: {target.name}")
    return {"backup": str(target), **counts}


def undo_run(conn, run_id: int, user: str) -> None:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Run not found.")
    last = latest_run(conn, run["account_id"])
    if last["id"] != run_id:
        raise ValueError("Only the latest reconciliation for an account can be undone. Undo later runs first.")
    with db.tx(conn):
        _delete_run(conn, run_id)
        prev = latest_run(conn, run["account_id"])
        if prev:
            refresh_run(conn, prev["id"])
        db.audit(conn, user, "undo_run", f"Run #{run_id} ({run['period_from']}..{run['period_to']}) undone")


def runs_to_revoke(conn, account_ids: list[int], from_date: date) -> list[dict]:
    """Runs covering `from_date` or later, for the given accounts (latest first per account)."""
    out = []
    for aid in account_ids:
        rows = conn.execute(
            """SELECT r.*, a.nickname, a.acc_no FROM runs r JOIN accounts a ON a.id=r.account_id
               WHERE r.account_id=? AND r.period_to>=? ORDER BY r.period_to DESC, r.id DESC""",
            (aid, from_date.isoformat())).fetchall()
        out += [dict(r) for r in rows]
    return out


def revoke_from(conn, account_ids: list[int], from_date: date, reason: str, user: str) -> list[dict]:
    """Admin: remove the reconciliation of `from_date` and every later day for these accounts.

    Later days must go too, because each day continues from the one before it. A copy of every removed run
    (BRS, flags, checks, matches and items) is kept in `revoked_runs`.
    """
    if len(reason.strip()) < MIN_REASON:
        raise ValueError(f"Give a proper reason (at least {MIN_REASON} characters).")
    batch = f"{db.now()} {user}"
    with _RUN_LOCK, db.tx(conn):
        runs = runs_to_revoke(conn, account_ids, from_date)
        if not runs:
            raise ValueError("Nothing to revoke: no reconciliation on or after that date.")
        for r in runs:
            snap = {
                "run": {k: r[k] for k in r if k not in ("nickname", "acc_no")},
                "matches": [dict(m) for m in conn.execute("SELECT * FROM matches WHERE run_id=?", (r["id"],))],
                "items": [dict(i) for i in conn.execute("SELECT * FROM items WHERE run_id=?", (r["id"],))],
            }
            conn.execute(
                """INSERT INTO revoked_runs (run_id, account_id, period_from, period_to, created_by, created_at,
                   revoked_at, revoked_by, reason, batch, snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["account_id"], r["period_from"], r["period_to"], r["created_by"], r["created_at"],
                 db.now(), user, reason.strip(), batch, db.dumps(snap)))
            _delete_run(conn, r["id"])
            db.audit(conn, user, "revoke_run",
                     f"Run #{r['id']} · {r['nickname']} · {r['period_from']}..{r['period_to']} · reason: {reason.strip()}")
        for aid in {r["account_id"] for r in runs}:
            prev = latest_run(conn, aid)
            if prev:
                refresh_run(conn, prev["id"])
    return runs


def recheck_account(conn, account_id: int, user: str) -> dict:
    """Apply newer matching rules to lines that are still open, without redoing any reconciliation.

    Existing matches (automatic and manual), reviews and sign-offs are kept. Only the newer passes run
    (branch names shortened/split, loan renewals settled net) over the account's currently open lines.
    Each new match is recorded on the run where both sides first existed, and every run from there on is
    recomputed, so earlier days show the corrected exceptions too. Lines a person unmatched are skipped."""
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    last = latest_run(conn, account_id)
    if not acc or not last:
        return {"account": acc["nickname"] if acc else "?", "new_matches": 0, "methods": {}, "open_before": 0,
                "open_after": 0}
    with _RUN_LOCK, db.tx(conn):
        blocked = {r["item_id"] for r in conn.execute("SELECT item_id FROM no_auto")}
        open_ = [i for i in open_items_as_of(conn, account_id, last["id"]) if i["id"] not in blocked]
        open_before = len(open_items_as_of(conn, account_id, last["id"]))
        for i in open_:
            i["carried"] = False
        bank = [i for i in open_ if i["side"] == "bank"]
        book = [i for i in open_ if i["side"] == "book"]
        branch_names = [n for r in conn.execute("SELECT branch, nickname FROM accounts")
                        for n in (r["branch"], r["nickname"]) if n]
        branch_names += [b.get("branch", "") for b in book if b.get("branch")]
        m = Matcher(bank, book, window_days=WINDOW_DAYS, branches=branch_names)
        m.sweep_pass("out")
        m.sweep_pass("in")
        m.renewal_pass()
        m.nach_pass()
        m.unique_amount_pass("in", min_amount=BIG_CREDIT, same_day_only=True)
        byk = {i["key"]: i for i in open_}
        first_run = None
        methods: dict[str, int] = defaultdict(int)
        for mt in m.matches:
            keys = [*mt["bank"], *mt["book"]]
            rid = max(byk[k]["run_id"] for k in keys)
            first_run = rid if first_run is None else min(first_run, rid)
            cur = conn.execute(
                "INSERT INTO matches (run_id, account_id, method, confidence, note, manual, created_at, created_by) "
                "VALUES (?,?,?,?,?,0,?,?)",
                (rid, account_id, mt["method"], mt["confidence"],
                 (mt["note"] + " · " if mt["note"] else "") + "found on re-check with updated rules",
                 db.now(), "system (re-check)"))
            conn.execute(f"UPDATE items SET match_id=? WHERE id IN ({','.join('?' * len(keys))})",
                         (cur.lastrowid, *[int(k) for k in keys]))
            methods[mt["method"]] += 1
        # payments paired on amount alone whose beneficiary is written in the voucher narration → confirmed
        for mt in conn.execute("SELECT * FROM matches WHERE account_id=? AND manual=0 AND confidence='review' "
                               "AND method LIKE 'Amount only%'", (account_id,)).fetchall():
            its = [row_to_item(r) for r in conn.execute("SELECT * FROM items WHERE match_id=?", (mt["id"],))]
            bs = [i for i in its if i["side"] == "bank"]
            ks = [i for i in its if i["side"] == "book"]
            if len(bs) == 1 and len(ks) == 1 and bs[0]["amount"] >= BIG_CREDIT and (why := named_in_narration(bs[0], ks[0])):
                kind = "Own-account transfer" if why.startswith("Transfer") else "Amount + name in narration"
                conn.execute("UPDATE matches SET method=?, confidence='strong', note=? WHERE id=?",
                             (kind + mt["method"][len("Amount only"):], why + " · confirmed on re-check", mt["id"]))
                methods[kind] += 1
                first_run = mt["run_id"] if first_run is None else min(first_run, mt["run_id"])
        if first_run is not None:
            for r in conn.execute("SELECT id FROM runs WHERE account_id=? AND id>=? ORDER BY id",
                                  (account_id, first_run)).fetchall():
                refresh_run(conn, r["id"])
        open_after = len(open_items_as_of(conn, account_id, last["id"]))
        summary = {"account": acc["nickname"], "new_matches": sum(methods.values()), "methods": dict(methods),
                   "open_before": open_before, "open_after": open_after}
        db.audit(conn, user, "recheck",
                 f"{acc['nickname']} (…{acc['acc_no'][-4:]}): {sum(methods.values())} new or confirmed match(es)"
                 + (" — " + ", ".join(f"{n} × {k}" for k, n in methods.items()) if methods else "")
                 + f"; open items {open_before} → {open_after}")
    return summary
