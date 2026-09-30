"""Reconciliation workflow: parse -> validate -> store -> match -> flags -> BRS.

All state lives in the database so that open items carry forward day to day,
and every run can be reproduced or undone (latest first, in order).
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import date, timedelta

from . import db
from .matcher import Matcher, build_flags, compute_brs, TOL
from .parsers import parse_bank_book, parse_bank_statement, file_hash, inr, check

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
        "seq": r.get("seq", ""), "text": r["narration"], "row_no": r["row_no"], "sl": r["sl"],
        "balance": r["balance"], "value_date": r["value_date"],
    }


def book_item(r: dict) -> dict:
    return {
        "side": "book", "dir": "out" if r["payment"] else "in",
        "date": r["date"], "amount": r["payment"] or r["receipt"],
        "name": r["name"], "ref": r["instrument"], "l4": r["l4"], "instr_no": r["instr_no"],
        "loan_ac": r["loan_ac"], "loan_type": r["loan_type"], "vno": r["vno"], "vtype": r["vtype"],
        "branch": r["branch"], "accounts": r["accounts"], "narration": r["narration"], "notes": r["notes"],
        "text": " ".join([r["narration"], r["instrument"], r["notes"], r["remarks"]]),
        "row_no": r["row_no"],
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


def net_open(items: list[dict]) -> float:
    """Effect of open items on (bank − book): + book payments − book receipts + bank credits − bank debits."""
    t = 0.0
    for i in items:
        if i["side"] == "book":
            t += i["amount"] if i["dir"] == "out" else -i["amount"]
        else:
            t += i["amount"] if i["dir"] == "in" else -i["amount"]
    return round(t, 2)


def latest_run(conn, account_id: int):
    return conn.execute("SELECT * FROM runs WHERE account_id=? ORDER BY period_to DESC, id DESC LIMIT 1",
                        (account_id,)).fetchone()


# ---------------------------------------------------------------------------- main entry

def reconcile(conn, account_id: int, bank_path: str, bank_name: str, book_path: str, book_name: str,
              user: str, acknowledged: bool = False, ack_reason: str = "") -> int:
    with _RUN_LOCK:
        return _reconcile(conn, account_id, bank_path, bank_name, book_path, book_name, user, acknowledged, ack_reason)


def _reconcile(conn, account_id, bank_path, bank_name, book_path, book_name, user, acknowledged, ack_reason) -> int:
    acc = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not acc:
        raise Blocked([check("error", "ACCOUNT", "Unknown account.")], [])

    bank = parse_bank_statement(bank_path)
    book = parse_bank_book(book_path)
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
    bh, kh = file_hash(bank_path), file_hash(book_path)
    for h, label in ((bh, "bank statement"), (kh, "bank book")):
        prev = conn.execute("SELECT r.id, r.period_to, a.nickname FROM runs r JOIN accounts a ON a.id=r.account_id "
                            "WHERE r.bank_hash=? OR r.book_hash=?", (h, h)).fetchone()
        if prev:
            add("error", "DUPLICATE_FILE",
                f"This exact {label} file was already used in run #{prev['id']} "
                f"({prev['nickname']}, {prev['period_to']}).")
    if bh == kh:
        add("error", "SAME_FILE", "The same file was uploaded as both bank statement and bank book.")

    # --- sequence and continuity with the previous run
    last = latest_run(conn, account_id)
    carried = []
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
        if abs(last["bank_close"] - bank["opening"]) > TOL:
            add("error", "BANK_CONTINUITY",
                f"Bank opening ₹{inr(bank['opening'])} ≠ previous bank closing ₹{inr(last['bank_close'])} "
                f"(difference ₹{inr(bank['opening'] - last['bank_close'])}). Either a day with transactions has "
                "not been reconciled, the statement is incomplete, or it is for a different account. "
                "This cannot be overridden — reconcile the missing day(s) first.")
        else:
            add("ok", "BANK_CONTINUITY", f"Bank opening ₹{inr(bank['opening'])} = previous closing.")
        if abs(last["book_close"] - book["opening"]) > TOL:
            add("error" if not acknowledged else "warning", "BOOK_CONTINUITY",
                f"Book opening ₹{inr(book['opening'])} ≠ previous book closing ₹{inr(last['book_close'])} "
                f"(difference ₹{inr(book['opening'] - last['book_close'])}). Usually a back-dated entry was posted "
                "in the ERP for a date already reconciled. Undo back to that date and re-run with fresh exports, "
                "or acknowledge to carry the difference as an unexplained item.",
                can_ack=True)
        else:
            add("ok", "BOOK_CONTINUITY", f"Book opening ₹{inr(book['opening'])} = previous closing.")
        carried = open_items_as_of(conn, account_id, last["id"])

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
    unexplained = round(bank["opening"] - book["opening"] - net_open(carried) - prior_diff, 2)
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
               checks_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, p_from.isoformat(), p_to.isoformat(), db.now(), user, bank_name, bh, book_name, kh,
             bank["opening"], bank["closing"], book["opening"], book["closing"], opening_added,
             ack_reason.strip(), db.dumps(all_checks)))
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
        for c in carried:
            c["carried"] = True

        bank_items = [i for i in carried + new_items if i["side"] == "bank"]
        book_items = [i for i in carried + new_items if i["side"] == "book"]
        matches = Matcher(bank_items, book_items, window_days=WINDOW_DAYS).run()
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
    flags = build_flags(bank_u, book_u, mlist, acc["kind"], as_of, aged_days=AGED_DAYS)
    flags = history_flags(conn, run, [i for i in items if i["run_id"] == run_id]) + flags
    brs = compute_brs(run["book_close"], run["bank_close"],
                      [o for o in open_now if o["side"] == "bank"],
                      [o for o in open_now if o["side"] == "book"],
                      cumulative_opening_diff(conn, run["account_id"], run_id))
    if abs(brs["opening_diff"]) > TOL:
        flags.insert(0, {"severity": "critical", "code": "UNEXPLAINED_DIFFERENCE",
                         "title": f"Unexplained difference of ₹{inr(brs['opening_diff'])} carried in the BRS",
                         "detail": "Accepted with an acknowledgement on an earlier run and not itemised. The BRS only "
                                   "balances because of it. Find the cause (missed entry, back-dated voucher) and correct it.",
                         "keys": []})
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
        conn.execute("UPDATE items SET match_id=NULL WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM matches WHERE id=?", (match_id,))
        refresh_run(conn, last["id"])
        db.audit(conn, user, "unmatch", f"Match #{match_id} ({m['method']}) removed · {reason.strip()}")


def add_note(conn, item_id: int, note: str, user: str) -> None:
    with db.tx(conn):
        conn.execute("UPDATE items SET note=? WHERE id=?", (note.strip(), item_id))
        db.audit(conn, user, "note", f"Item #{item_id}: {note.strip()}")


def undo_run(conn, run_id: int, user: str) -> None:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError("Run not found.")
    last = latest_run(conn, run["account_id"])
    if last["id"] != run_id:
        raise ValueError("Only the latest reconciliation for an account can be undone. Undo later runs first.")
    with db.tx(conn):
        conn.execute("UPDATE items SET match_id=NULL WHERE match_id IN (SELECT id FROM matches WHERE run_id=?)", (run_id,))
        conn.execute("DELETE FROM matches WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM items WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        prev = latest_run(conn, run["account_id"])
        if prev:
            refresh_run(conn, prev["id"])
        db.audit(conn, user, "undo_run", f"Run #{run_id} ({run['period_from']}..{run['period_to']}) undone")
