"""Rebuild saved reconciliations from the files the server kept, with the current matching rules.

Used once (21-11-2026) after the after-hours / sweep / cash / JLG rules were added, so the days reconciled
since the 16-11 fresh start look as if they had been uploaded today. For every account:

1. Plan: the run's bank statement and bank book(s) must still be in data/uploads/<run id>/. Runs that used the
   back-dated finder are not rebuilt (that extra export is not kept).
2. Rehearse on a copy of the database: remove the account's runs and reconcile each day again, in date order,
   as the person who uploaded it, with the reason they gave for any override. Any day that stops (a check that
   now needs a person) leaves the account as it is.
3. Only accounts that rehearse cleanly are rebuilt in the live database. The removed runs are kept in
   revoked_runs; the audit log and one inbox message say what was rebuilt and what was left.

Review notes, sign-offs and manual matches on the rebuilt days are not carried over (their lines are new).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from . import db, engine

REASON = "Rebuilt automatically with the 19-11-2026 rules (after-hours credits, sweeps, cash deposits, JLG groups)"


def _open(path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def plan_account(conn, account_id: int, uploads: Path) -> tuple[list[dict], str]:
    """The days to replay, oldest first, or ([], why not)."""
    steps = []
    for r in conn.execute("SELECT * FROM runs WHERE account_id=? ORDER BY period_to, id", (account_id,)):
        folder = uploads / str(r["id"])
        names = [n for n in (r["book_file"] or "").split(" + ") if n]
        bank = folder / (r["bank_file"] or "")
        books = [folder / n for n in names]
        day = f"{date.fromisoformat(r['period_to']):%d-%m-%Y}"
        if not r["bank_file"] or not names or r["bank_file"] in names:
            return [], f"{day}: the statement and bank book file names cannot be told apart"
        if not bank.is_file() or not all(b.is_file() for b in books):
            return [], f"{day}: the uploaded files were not kept on the server"
        if conn.execute("SELECT 1 FROM items WHERE run_id=? AND data_json LIKE '%\"backdated\": true%'",
                        (r["id"],)).fetchone():
            return [], f"{day}: used the back-dated finder (that extra bank book export is not kept)"
        steps.append({"old_id": r["id"], "day": day, "period_to": r["period_to"], "bank": str(bank),
                      "bank_name": r["bank_file"], "books": [str(b) for b in books], "book_name": r["book_file"],
                      "user": r["created_by"], "ack": (r["ack_reason"] or "").strip()})
    return steps, ""


def replay(conn, account_id: int, steps: list[dict]) -> list[tuple[int, int]]:
    """Remove the account's runs and reconcile every day again. Raises engine.Blocked / ValueError on a stop."""
    engine.revoke_from(conn, [account_id], date.fromisoformat(steps[0]["period_to"]), REASON, "system")
    done = []
    for s in steps:
        book = s["books"][0] if len(s["books"]) == 1 else s["books"]
        try:
            rid = engine.reconcile(conn, account_id, s["bank"], s["bank_name"], book, s["book_name"], s["user"],
                                   acknowledged=bool(s["ack"]), ack_reason=s["ack"], confirmed=True)
        except engine.Blocked as e:
            stop = [c for c in e.checks if c["level"] == "error"] or e.ackable or e.checks
            raise ValueError(f"{s['day']}: {stop[0]['message'][:220]}") from None
        done.append((s["old_id"], rid))
    return done


def run(conn, uploads: Path, label: str) -> dict:
    """Rehearse on a copy, then rebuild the accounts that went through. Returns a summary."""
    accounts = [dict(a) for a in conn.execute(
        "SELECT a.id, a.nickname, a.acc_no FROM accounts a WHERE EXISTS (SELECT 1 FROM runs r WHERE r.account_id=a.id) "
        "ORDER BY a.id")]
    rebuilt, left = [], []
    if not accounts:
        return {"rebuilt": rebuilt, "left": left}
    with tempfile.TemporaryDirectory() as tmp:
        copy = _open(Path(tmp) / "rehearsal.sqlite3")
        conn.backup(copy)
        ok_plans = {}
        for a in accounts:
            name = f"{a['nickname']} (…{a['acc_no'][-4:]})"
            steps, why = plan_account(copy, a["id"], uploads)
            if not steps:
                left.append(f"{name}: {why}")
                continue
            try:
                replay(copy, a["id"], steps)
                ok_plans[a["id"]] = (name, steps)
            except Exception as e:  # stop on this account only
                left.append(f"{name}: {e}")
        copy.close()
    first_alert = conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0]
    for aid, (name, steps) in ok_plans.items():
        try:
            done = replay(conn, aid, steps)
        except Exception as e:  # should not happen after a clean rehearsal; keep what is there and report it
            left.append(f"{name}: stopped while rebuilding ({e}); check this account's History")
            continue
        for old, new in done:  # the kept files follow the new run
            src, dst = uploads / str(old), uploads / str(new)
            if src.is_dir() and not dst.exists():
                shutil.move(str(src), str(dst))
        open_now = conn.execute("SELECT COUNT(*) FROM items WHERE account_id=? AND match_id IS NULL",
                                (aid,)).fetchone()[0]
        rebuilt.append(f"{name}: {len(done)} day(s) {steps[0]['day']} to {steps[-1]['day']}, {open_now} line(s) open now")
    with db.tx(conn):
        # the per-day inbox messages the rebuild re-created are not news: mark them read for everyone who has an inbox
        users = [u["username"] for u in conn.execute("SELECT username, role FROM users")
                 if u["role"] in ("manager", "admin")]
        for (aid_,) in conn.execute("SELECT id FROM alerts WHERE id>?", (first_alert,)).fetchall():
            for u in users:
                conn.execute("INSERT OR IGNORE INTO alert_reads (alert_id, username, read_at) VALUES (?,?,?)",
                             (aid_, u, db.now()))
        body = ("Rebuilt:\n" + "\n".join(f"· {x}" for x in rebuilt) if rebuilt else "Nothing was rebuilt.") + \
               ("\n\nLeft as it was (re-upload these days, or send the files):\n" + "\n".join(f"· {x}" for x in left)
                if left else "")
        conn.execute("INSERT INTO alerts (created_at, account_id, run_id, severity, title, body, created_by) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (db.now(), None, None, "warning" if left else "info",
                      f"Reconciliations rebuilt with the new rules: {len(rebuilt)} account(s)"
                      + (f", {len(left)} left as they were" if left else ""), body, "system"))
        db.audit(conn, "system", "rebuild_" + label, body[:4000])
    return {"rebuilt": rebuilt, "left": left}
