"""Bulk upload: many bank statements and bank books at once.

Each file is classified (statement or bank book), tied to an account, and paired by account and date.
Complete pairs are reconciled in date order per account; anything that cannot be identified or is missing
its other half is listed so the user can assign it or upload it.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .matcher import branch_score, clean_name
from .parsers import parse_bank_book, parse_bank_statement, read_sheet, _find_header, TOL

BOOK_HEADER = [["vchrnum", "voucherno", "vchrno", "voucher"], ["debit", "dr"], ["credit", "cr"], ["narration"]]


def _norm_branch(s: str) -> str:
    return re.sub(r"[^A-Z]", "", str(s or "").upper())


def classify(path: str, name: str) -> dict:
    """Read one file: what it is, its dates and the facts used to identify its account."""
    info = {"kind": "unknown", "date_from": None, "date_to": None, "acc_digits": "", "branches": [],
            "opening": None, "error": ""}
    try:
        df = read_sheet(path)
    except Exception as e:  # unreadable
        info["error"] = f"Could not open the file ({e})."
        return info
    if _find_header(df, BOOK_HEADER) is not None:
        book = parse_bank_book(path)
        info.update(kind="book", date_from=book.get("period_from"), date_to=book.get("period_to"),
                    opening=book.get("opening"))
        info["branches"] = [b["branch"] for b in book.get("branches", [])
                            if b.get("count") or abs(b.get("opening") or 0) > TOL or abs(b.get("closing") or 0) > TOL]
        if not book.get("ok"):
            info["error"] = "; ".join(c["message"] for c in book.get("checks", []) if c["level"] == "error")
        return info
    st = parse_bank_statement(path)
    if st.get("checks") and any(c["code"] in ("BANK_HEADER", "BANK_COLUMNS") for c in st["checks"]):
        info["error"] = "Not a bank statement or an ERP bank book."
        return info
    dates = sorted({r["date"] for r in st.get("rows", [])})
    info.update(kind="statement", date_from=dates[0] if dates else None, date_to=dates[-1] if dates else None,
                opening=st.get("opening"))
    digits = re.sub(r"\D", "", st.get("account_no") or "")
    if len(digits) < 4:
        found = re.findall(r"(\d{4,})", re.sub(r"\d{1,2}-\d{1,2}-\d{4}", "", name))  # ignore dates in the name
        digits = found[-1] if found else ""
    info["acc_digits"] = digits
    if not st.get("ok"):
        info["error"] = "; ".join(c["message"] for c in st.get("checks", []) if c["level"] == "error")
    return info


def identify(conn, info: dict, accounts: list[dict]) -> tuple[int | None, str]:
    """(account id, how it was identified) — or (None, why not)."""
    last = {}
    for a in accounts:
        r = conn.execute("SELECT * FROM runs WHERE account_id=? ORDER BY period_to DESC, id DESC LIMIT 1",
                         (a["id"],)).fetchone()
        last[a["id"]] = r
    if info["kind"] == "statement":
        d = info["acc_digits"]
        if len(d) >= 4:
            def common(a):
                ad, n = re.sub(r"\D", "", a["acc_no"]), 0
                while n < min(len(ad), len(d)) and ad[-1 - n] == d[-1 - n]:
                    n += 1
                return n
            scored = sorted(((common(a), a) for a in accounts), key=lambda x: -x[0])
            if scored and scored[0][0] >= 4 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                return scored[0][1]["id"], f"account number …{d[-4:]}"
        if info["opening"] is not None:
            hits = [a for a in accounts if last[a["id"]] and abs(last[a["id"]]["bank_close"] - info["opening"]) <= TOL]
            if len(hits) == 1:
                return hits[0]["id"], "opening balance = last bank closing"
        return None, "No account number in the file, and its opening balance matches no account's last closing."
    if info["kind"] == "book":
        active = info["branches"]
        if not active:
            return None, "The bank book has no active branch."
        names = {_norm_branch(b) for b in active}
        cands = [a for a in accounts if _norm_branch(a.get("branch")) in names]
        if not cands:
            scored = [(max(branch_score(clean_name(a.get("branch") or a["nickname"]), clean_name(b)) for b in active), a)
                      for a in accounts]
            top = [a for s, a in scored if s >= 85]
            cands = top
        if len(cands) == 1:
            return cands[0]["id"], f"branch {cands[0].get('branch')}"
        pool = cands or accounts
        hits = [a for a in pool if last[a["id"]] and info["opening"] is not None
                and abs(last[a["id"]]["book_close"] - info["opening"]) <= TOL]
        if len(hits) == 1:
            return hits[0]["id"], (f"branch {hits[0].get('branch')} + " if cands else "") + "opening = last book closing"
        if len(cands) > 1:
            return None, (f"Branch {', '.join(active)} has {len(cands)} accounts ("
                          + ", ".join(f"…{a['acc_no'][-4:]}" for a in cands) + "). Choose which one.")
        return None, f"No account is set up for branch {', '.join(active)}."
    return None, "Not a bank statement or an ERP bank book."


def plan(files: list[dict]) -> dict:
    """Pair identified files by account and date. Returns pairs (sorted per account by date),
    files still missing their other half, and duplicates."""
    by = defaultdict(lambda: {"statement": [], "book": []})
    for f in files:
        if f.get("account_id") and f["kind"] in ("statement", "book"):
            by[f["account_id"]][f["kind"]].append(f)
    pairs, lonely, dupes = [], [], []
    for aid, g in by.items():
        books = sorted(g["book"], key=lambda f: f["date_from"] or "")
        stmts = sorted(g["statement"], key=lambda f: f["date_from"] or "")
        used = set()
        for b in books:
            match = [s for s in stmts if s["fid"] not in used and s["date_from"] == b["date_from"]]
            if not match:  # statement of a day with no transactions has no date
                match = [s for s in stmts if s["fid"] not in used and not s["date_from"]]
            if match:
                s = match[0]
                used.add(s["fid"])
                key = (aid, b["date_from"])
                if any((p["account_id"], p["date"]) == key for p in pairs):
                    dupes += [s, b]
                    continue
                pairs.append({"account_id": aid, "date": b["date_from"], "stmt": s["fid"], "book": b["fid"]})
            else:
                lonely.append(dict(b, needs="statement"))
        for s in stmts:
            if s["fid"] not in used:
                lonely.append(dict(s, needs="book"))
    pairs.sort(key=lambda p: (p["account_id"], p["date"] or ""))
    return {"pairs": pairs, "lonely": lonely, "dupes": dupes}
