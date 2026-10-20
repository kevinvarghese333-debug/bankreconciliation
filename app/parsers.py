"""Readers for bank statements and the ERP bank book.

Each parser returns a plain dict with the parsed rows, the balances it found,
and a list of validation checks. A check is a dict:
    {"level": "ok" | "warning" | "error", "code": str, "message": str}
"error" means the file cannot be trusted and the reconciliation must not run.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

TOL = 0.005  # paise tolerance for float arithmetic


# --------------------------------------------------------------------------- helpers

def file_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def to_amount(v) -> float:
    """'1,90,57,109.27' -> 19057109.27 ; blanks -> 0.0 ; '1,000.00 Dr' -> -1000.0"""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return 0.0 if pd.isna(v) else round(float(v), 2)
    s = str(v).strip()
    if not s or s.lower() in ("nan", "-", "none"):
        return 0.0
    neg = False
    if s.upper().endswith("DR"):
        neg, s = True, s[:-2]
    elif s.upper().endswith("CR"):
        s = s[:-2]
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace(",", "").replace("₹", "").replace("INR", "").strip()
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    val = round(float(s), 2)
    return -val if neg else val


_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%m-%y", "%d/%m/%y",
                 "%Y-%m-%d", "%d.%m.%Y", "%d-%b-%y")


def to_date(v) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, pd.Timestamp):
        return v.date()
    s = str(v).strip().split(" ")[0] if re.match(r"^\d{4}-\d{2}-\d{2} ", str(v)) else str(v).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def read_sheet(path: str | Path) -> pd.DataFrame:
    """Read the first sheet of .xls / .xlsx / .csv with no header inference."""
    p = str(path).lower()
    if p.endswith(".csv"):
        # Bank CSVs (ICICI) carry a title line, blank lines and a footer: rows of different widths
        import csv
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        rows = list(csv.reader(text.splitlines()))
        width = max((len(r) for r in rows), default=0)
        rows = [[(c.strip() if c.strip() != "" else None) for c in r] + [None] * (width - len(r)) for r in rows]
        return pd.DataFrame(rows, dtype=object)
    if p.endswith(".xls"):
        try:
            return pd.read_excel(path, header=None, dtype=object, engine="xlrd")
        except Exception:
            # Some banks ship HTML tables with an .xls extension
            tables = pd.read_html(path)
            return tables[0]
    return pd.read_excel(path, header=None, dtype=object, engine="openpyxl")


def _find_header(df: pd.DataFrame, must: list[list[str]], max_rows: int = 40) -> int | None:
    """First row index whose normalised cells contain one alias from every group in `must`."""
    for i in range(min(max_rows, len(df))):
        cells = [norm(c) for c in df.iloc[i].tolist() if not pd.isna(c)]
        if all(any(any(a == c or c.startswith(a) for c in cells) for a in group) for group in must):
            return i
    return None


def _col(header: list[str], aliases: list[str], exclude: tuple[int, ...] = ()) -> int | None:
    for a in aliases:
        for idx, h in enumerate(header):
            if idx in exclude:
                continue
            if h == a:
                return idx
    for a in aliases:
        for idx, h in enumerate(header):
            if idx in exclude:
                continue
            if h.startswith(a):
                return idx
    return None


def inr(x: float) -> str:
    """Indian digit grouping: 19056309.27 -> '1,90,56,309.27'."""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        whole = _grp(head) + "," + tail
    return ("-" if neg else "") + whole + "." + frac


def _grp(head: str) -> str:
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts)


def check(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


# --------------------------------------------------------------------------- bank narration

UTR_RE = re.compile(r"(?<!\d)(\d{12})(?!\d)")


def parse_narration(text: str) -> dict:
    """Pull channel, reference and counter-party name out of a bank narration.

    SIB formats seen:
      RAP/<utr>/ <payment seq> /<beneficiary>/          (API disbursal)
      UPI/<bank>/<utr>/<payer>/<note>/<txn id>/<vpa>     (UPI collection)
      <NAME>/                                            (truncated RAP)
    Anything else: NEFT/IMPS/RTGS/CLG etc. handled generically.
    """
    t = str(text or "").strip()
    parts = [p.strip() for p in t.split("/")]
    head = parts[0].upper() if parts else ""
    up = t.upper()
    out = {"channel": "", "ref": "", "seq": "", "name": "", "branch": ""}
    # Day-end sweep from a collection account: "TR FROM CHERAI BRANCH/", "TR FRM EDAVANAKKAD BRANCH 73.1138/",
    # "FROM MANJAPRA BRANCH/", "TR FROM 58.73.1156 THIRUVANKULAM BRANCH/", "TR FROM KUNNUKARA BRA/"
    m = re.match(r"^\s*(?:TR\.?\s*)?(?:FROM|FRM)\s+(.+?)\s*/?\s*$", up)
    if m and "/" not in m.group(1):
        br = re.sub(r"[\d.]+", " ", m.group(1))
        br = re.sub(r"\b(BRANCH|BRA|BR)\b", " ", br)
        br = re.sub(r"\s+", " ", br).strip()
        acc = re.search(r"(\d{2}\.\d{2}\.)?(\d{4})\b", m.group(1))
        out.update(channel="SWEEP", name=br, branch=br, ref=acc.group(2) if acc else "")
        return out
    if up.startswith("BY CASH"):
        br = re.search(r"BR:\s*([^/]+)", up)
        out.update(channel="CASH", name="CASH DEPOSIT" + (f" {br.group(1).strip()}" if br else ""),
                   branch=br.group(1).strip() if br else "")
        return out
    if up.startswith("RTGS") or up.startswith("NEFT TO") or up.startswith("NEFT FROM"):
        # "RTGS TO:SIBLH26272448778:K O VARGHESE/"
        bits = [b.strip() for b in re.split(r"[:/]", t) if b.strip()]
        ref = next((b for b in bits if re.fullmatch(r"[A-Z]{4}[A-Z0-9]{8,}|\d{12,}", b.upper())), "")
        names = [b for b in bits[1:] if b != ref and re.search(r"[A-Za-z]{3,}", b)]
        out.update(channel=up.split()[0].split(":")[0], ref=ref, name=names[-1] if names else "")
        return out
    if head == "IB" and len(parts) >= 3:
        # "IB/KUTHIYATHODE/CHIEF ENGINEER( II , CR AND CAPS)/INTER BRANCH"
        out.update(channel="IB", branch=parts[1], name=parts[2])
        out["name"] = re.sub(r"\s+", " ", out["name"]).strip()
        return out
    if head.startswith("BILLDESK"):
        m2 = re.match(r"BILLDESK-(.+?)-(\d+)-", t, re.I)
        out.update(channel="BILLDESK", name=m2.group(1) if m2 else t, ref=m2.group(2) if m2 else "")
        return out
    if head == "RAP" and len(parts) >= 3 and parts[2].upper().startswith("RAP-REV"):
        # returned / reversed API disbursal: "RAP/<utr>/RAP-REV/ /SHAHANABI FI/6272151413/"
        names = [x for x in parts[3:] if re.search(r"[A-Za-z]{2,}", x)]
        out.update(channel="RAP-REV", ref=parts[1], name=names[0] if names else "")
        out["name"] = re.sub(r"\s+", " ", out["name"]).strip()
        return out
    if head == "RAP" and len(parts) >= 4:
        out.update(channel="RAP", ref=parts[1], seq=parts[2].lstrip("0"), name=parts[3])
    elif head == "UPI" and len(parts) >= 3 and re.fullmatch(r"\d{10,}", parts[1] or ""):
        # ICICI: UPI/<utr>/<note>/<masked vpa>/<payer bank>/<txn id> — no payer name
        out.update(channel="UPI", ref=parts[1], name="")
    elif head == "UPI" and len(parts) >= 4:
        out.update(channel="UPI", ref=parts[2], name=parts[3])
    elif re.search(r"\bNACH\d*", up):
        m = re.search(r"(NACH\d*)", up)
        out.update(channel="NACH", ref=m.group(1), name="")
    elif head in ("NEFT", "IMPS", "RTGS", "MMT", "INF", "INFT", "NFT"):
        out["channel"] = head
        m = UTR_RE.search(t) or re.search(r"([A-Z]{4}[A-Z0-9]{8,})", t)
        out["ref"] = m.group(1) if m else (parts[1] if len(parts) > 1 else "")
        names = [p for p in parts[2:] if re.search(r"[A-Za-z]{3,}", p)]
        out["name"] = names[0] if names else ""
    else:
        m = UTR_RE.search(t)
        out["ref"] = m.group(1) if m else ""
        letters = [p for p in parts if re.search(r"[A-Za-z]{3,}", p)]
        out["name"] = letters[0] if letters else t
        out["channel"] = "OTHER"
    out["name"] = re.sub(r"\s+", " ", out["name"]).strip()
    return out


# --------------------------------------------------------------------------- bank statement

def parse_bank_statement(path: str | Path) -> dict:
    checks: list[dict] = []
    df = read_sheet(path)
    hdr = _find_header(df, [["particulars", "narration", "description", "remarks", "transactiondetails", "tranparticular"],
                            ["balance", "balanceamount", "closingbalance", "balamt"],
                            ["withdrawal", "withdrawals", "debit", "dr", "withdrawalamt", "drtranamt"]])
    if hdr is None:
        return {"ok": False, "checks": [check("error", "BANK_HEADER",
                "Could not find the column header row (Particulars / Withdrawals / Deposits / Balance) "
                "in the bank statement. Is this the right file?")], "rows": []}
    header = [norm(c) if not pd.isna(c) else "" for c in df.iloc[hdr].tolist()]
    c_date = _col(header, ["transactiondate", "txndate", "trandate", "date", "postdate"])
    c_vdate = _col(header, ["valuedate", "valuedt"])
    c_narr = _col(header, ["particulars", "narration", "description", "transactiondetails", "tranparticular", "remarks"])
    c_chq = _col(header, ["chequenumber", "chequeno", "chqno", "chq", "refno", "cheque", "instnum"])
    c_wd = _col(header, ["withdrawals", "withdrawal", "withdrawalamt", "debit", "dr", "drtranamt"])
    c_dp = _col(header, ["deposits", "deposit", "depositamt", "credit", "cr", "crtranamt"])
    c_bal = _col(header, ["balanceamount", "balance", "closingbalance", "balamt"])
    c_sl = _col(header, ["slno", "srno", "sno", "serialno"])
    missing = [n for n, c in [("date", c_date), ("narration", c_narr), ("withdrawals", c_wd),
                              ("deposits", c_dp), ("balance", c_bal)] if c is None]
    if missing:
        return {"ok": False, "rows": [], "checks": [check("error", "BANK_COLUMNS",
                f"Bank statement is missing column(s): {', '.join(missing)}.")]}

    # account number, if printed above the header
    acc_no = ""
    for i in range(hdr):
        line = " ".join(str(c) for c in df.iloc[i].tolist() if not pd.isna(c))
        m = re.search(r"(?:a/?c|account)\s*(?:no\.?|number)?\s*[:\-]?\s*([0-9Xx*]{6,})", line, re.I)
        if m:
            acc_no = m.group(1)
            break

    if not acc_no:
        for i in range(hdr + 1, min(hdr + 6, len(df))):
            for c in df.iloc[i].tolist():
                m = re.match(r"^\s*a/?c\s*no\.?\s*:?\s*([0-9Xx*]{6,})\s*$", str(c or ""), re.I)
                if m:
                    acc_no = m.group(1)
                    break
            if acc_no:
                break

    rows = []
    bad_rows = []
    brought_forward = None   # ICICI "B/F" line: the opening balance printed by the bank
    grand = None          # the statement's own "Grand Total" row, when it prints one
    footer = 0
    for i in range(hdr + 1, len(df)):
        r = df.iloc[i].tolist()
        raw_date = r[c_date]
        narr = "" if pd.isna(r[c_narr]) else str(r[c_narr]).strip()
        joined = " ".join(str(x) for x in r if not pd.isna(x))
        low = joined.lower()
        if re.match(r"^\s*grand\s*total", low):
            # SIB mini statement: transactions end here; what follows is the bank's footer text
            try:
                grand = {"row_no": i + 1,
                         "withdrawal": abs(to_amount(r[c_wd])) if not pd.isna(r[c_wd]) else None,
                         "deposit": abs(to_amount(r[c_dp])) if not pd.isna(r[c_dp]) else None,
                         "balance": to_amount(r[c_bal]) if not pd.isna(r[c_bal]) else None}
            except ValueError:
                grand = {"row_no": i + 1, "withdrawal": None, "deposit": None, "balance": None}
            break
        if (pd.isna(raw_date) or str(raw_date).strip() == "") and not narr:
            if "end of" in low or not joined.strip():
                continue
        if narr.upper() in ("B/F", "BALANCE B/F", "OPENING BALANCE", "BROUGHT FORWARD"):
            try:
                brought_forward = to_amount(r[c_bal])
            except ValueError:
                bad_rows.append(i + 1)
            continue
        d = to_date(raw_date)
        if d is None:
            has_amount = any(not pd.isna(r[c]) and re.search(r"[1-9]", str(r[c])) for c in (c_wd, c_dp))
            if not has_amount:
                # a line of text with no money on it (bank's notes, page footer): nothing to reconcile
                footer += bool(joined.strip())
                continue
            bad_rows.append(i + 1)
            continue
        try:
            wd = abs(to_amount(r[c_wd]))
            dp = abs(to_amount(r[c_dp]))
            bal = to_amount(r[c_bal])
        except ValueError:
            bad_rows.append(i + 1)
            continue
        info = parse_narration(narr)
        chq = "" if c_chq is None or pd.isna(r[c_chq]) else str(r[c_chq]).strip()
        if chq.endswith(".0"):
            chq = chq[:-2]
        rows.append({
            "row_no": i + 1,
            "sl": "" if c_sl is None or pd.isna(r[c_sl]) else str(r[c_sl]).strip(),
            "date": d,
            "value_date": to_date(r[c_vdate]) if c_vdate is not None else d,
            "narration": narr,
            "cheque": chq,
            "withdrawal": wd,
            "deposit": dp,
            "balance": bal,
            **info,
        })

    if bad_rows:
        checks.append(check("error", "BANK_UNREADABLE_ROWS",
                            f"{len(bad_rows)} row(s) could not be read (Excel rows {bad_rows[:10]}). "
                            "The statement may be edited or corrupted."))
    if not rows:
        checks.append(check("error", "BANK_EMPTY", "No transactions found in the bank statement."))
        return {"ok": False, "rows": [], "checks": checks}

    # Some banks list newest first: detect by checking continuity both ways
    def breaks(seq):
        out = []
        for a, b in zip(seq, seq[1:]):
            exp = round(a["balance"] - b["withdrawal"] + b["deposit"], 2)
            if abs(exp - b["balance"]) > TOL:
                out.append((a, b, exp))
        return out

    def newest_first(seq):
        if seq[0]["date"] != seq[-1]["date"]:
            return seq[0]["date"] > seq[-1]["date"]
        sls = [int(r["sl"]) for r in seq if str(r["sl"]).isdigit()]
        return len(sls) >= 2 and sls[0] > sls[-1]

    if newest_first(rows):
        rows = rows[::-1]
        checks.append(check("ok", "BANK_ORDER", "Statement was in newest-first order; re-ordered oldest-first."))
    fwd = breaks(rows)

    both = [r for r in rows if r["withdrawal"] > 0 and r["deposit"] > 0]
    none = [r for r in rows if r["withdrawal"] == 0 and r["deposit"] == 0]
    if both:
        checks.append(check("error", "BANK_BOTH_SIDES",
                            f"{len(both)} row(s) have both a withdrawal and a deposit (Excel rows "
                            f"{[r['row_no'] for r in both[:10]]})."))
    if none:
        checks.append(check("warning", "BANK_ZERO_ROWS",
                            f"{len(none)} row(s) have no amount (Excel rows {[r['row_no'] for r in none[:10]]}); ignored."))
        rows = [r for r in rows if r not in none]

    if fwd:
        sample = "; ".join(f"row {b['row_no']}: expected {inr(exp)}, statement shows {inr(b['balance'])}"
                           for a, b, exp in fwd[:5])
        checks.append(check("error", "BANK_BALANCE_BREAK",
                            f"Running balance breaks at {len(fwd)} place(s) — {sample}. "
                            "Rows may be missing or the file has been altered."))
    else:
        checks.append(check("ok", "BANK_BALANCE_CHAIN",
                            f"Running balance verified on all {len(rows)} rows."))

    dates = [r["date"] for r in rows]
    if any(b < a for a, b in zip(dates, dates[1:])):
        checks.append(check("error", "BANK_DATE_ORDER",
                            "Transaction dates are not in order. Rows may have been moved or edited."))

    first = rows[0]
    opening = round(first["balance"] + first["withdrawal"] - first["deposit"], 2)
    closing = rows[-1]["balance"]
    tot_wd = round(sum(r["withdrawal"] for r in rows), 2)
    tot_dp = round(sum(r["deposit"] for r in rows), 2)
    if brought_forward is not None:
        if abs(brought_forward - opening) > TOL:
            checks.append(check("error", "BANK_BF",
                                f"Opening balance printed by the bank (B/F) {inr(brought_forward)} ≠ first row's opening "
                                f"{inr(opening)}. Rows may be missing at the start."))
        else:
            checks.append(check("ok", "BANK_BF", f"Opening balance B/F {inr(brought_forward)} agrees with the first row."))
    if grand:
        diffs = [f"{label}: statement total {inr(g)}, transactions add up to {inr(c)}"
                 for label, g, c in (("withdrawals", grand["withdrawal"], tot_wd), ("deposits", grand["deposit"], tot_dp),
                                     ("closing balance", grand["balance"], closing))
                 if g is not None and abs(g - c) > TOL]
        if diffs:
            checks.append(check("error", "BANK_GRAND_TOTAL",
                                f"The statement's Grand Total (Excel row {grand['row_no']}) does not agree — "
                                + "; ".join(diffs) + ". Rows may be missing or edited."))
        else:
            checks.append(check("ok", "BANK_GRAND_TOTAL",
                                f"Grand Total row agrees with the transactions (withdrawals {inr(tot_wd)}, "
                                f"deposits {inr(tot_dp)}, closing {inr(closing)}). Bank's footer notes below it ignored."))
    if abs(opening - tot_wd + tot_dp - closing) > TOL:
        checks.append(check("error", "BANK_TOTALS",
                            f"Opening {inr(opening)} − withdrawals {inr(tot_wd)} + deposits {inr(tot_dp)} "
                            f"≠ closing {inr(closing)}."))
    else:
        checks.append(check("ok", "BANK_TOTALS",
                            f"Opening {inr(opening)} − withdrawals {inr(tot_wd)} + deposits {inr(tot_dp)} "
                            f"= closing {inr(closing)}."))

    return {
        "ok": not any(c["level"] == "error" for c in checks),
        "rows": rows,
        "checks": checks,
        "account_no": acc_no,
        "opening": opening,
        "closing": closing,
        "total_withdrawals": tot_wd,
        "total_deposits": tot_dp,
        "period_from": min(dates),
        "period_to": max(dates),
    }


# --------------------------------------------------------------------------- ERP bank book

NAME_RE = re.compile(r"Name:\s*(.+?)\s*(?:,\s*[A-Z][\w ./]*:|$)")
LOANAC_RE = re.compile(r"A/c No:\s*([0-9]+)")
LOANTYPE_RE = re.compile(r"A/c:\s*([A-Z0-9 %]+?),")
PERIOD_RE = re.compile(r"from\s+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\s+to\s+(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})", re.I)
SUMMARY_WORDS = {"opening", "total", "closing"}


INSTR_DATE_RE = re.compile(r"Dt\.?\s*:?\s*(\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})")


def parse_bank_book(path: str | Path) -> dict:
    checks: list[dict] = []
    df = read_sheet(path)
    hdr = _find_header(df, [["vchrnum", "voucherno", "vchrno", "voucher"],
                            ["debit", "dr"], ["credit", "cr"], ["narration"]])
    if hdr is None:
        return {"ok": False, "rows": [], "checks": [check("error", "BOOK_HEADER",
                "Could not find the bank book header row (Tran.Date / Vchr.Num. / Debit / Credit). "
                "Is this the ERP Bank Book export?")]}

    title = " ".join(str(c) for i in range(hdr) for c in df.iloc[i].tolist() if not pd.isna(c))
    company = title.strip().split("\n")[0].strip() if title else ""
    pm = PERIOD_RE.search(title)
    p_from = to_date(pm.group(1).replace("/", "-")) if pm else None
    p_to = to_date(pm.group(2).replace("/", "-")) if pm else None

    header = [norm(c) if not pd.isna(c) else "" for c in df.iloc[hdr].tolist()]
    col = {
        "br_id": _col(header, ["brid", "branchid"]),
        "branch": _col(header, ["branch", "branchname"], exclude=tuple(
            i for i, h in enumerate(header) if h in ("brid", "brcode"))),
        "date": _col(header, ["trandate", "transactiondate", "date", "vchrdate"]),
        "vno": _col(header, ["vchrnum", "voucherno", "vchrno", "voucher"]),
        "vtype": _col(header, ["vchrtype", "vouchertype"]),
        "accounts": _col(header, ["accounts", "account", "particulars"]),
        "instr": _col(header, ["instrumentdet", "instrument", "chequeno", "refno"]),
        "narr": _col(header, ["narration"]),
        "notes": _col(header, ["notes", "note"]),
        "debit": _col(header, ["debit", "dr"]),
        "credit": _col(header, ["credit", "cr"]),
        "balance": _col(header, ["balance"]),
        "remarks": _col(header, ["remarks"]),
    }
    missing = [k for k in ("date", "narr", "debit", "credit") if col[k] is None]
    if missing:
        return {"ok": False, "rows": [], "checks": [check("error", "BOOK_COLUMNS",
                f"Bank book is missing column(s): {', '.join(missing)}.")]}

    def cell(r, k):
        i = col[k]
        if i is None:
            return ""
        v = r[i]
        return "" if pd.isna(v) else v

    branches: dict[str, dict] = {}
    rows: list[dict] = []
    bad = []
    for i in range(hdr + 1, len(df)):
        r = df.iloc[i].tolist()
        if all(pd.isna(x) or str(x).strip() == "" for x in r):
            continue
        narr = str(cell(r, "narr")).strip()
        bkey = f"{cell(r, 'br_id')}|{cell(r, 'branch')}".strip("|") or "ALL"
        b = branches.setdefault(bkey, {"branch": str(cell(r, "branch")) or "—", "br_id": cell(r, "br_id"),
                                       "opening": None, "total_dr": None, "total_cr": None,
                                       "closing": None, "sum_dr": 0.0, "sum_cr": 0.0, "count": 0,
                                       "run_breaks": 0, "_run": None})
        try:
            dr = to_amount(cell(r, "debit"))
            cr = to_amount(cell(r, "credit"))
            bal = to_amount(cell(r, "balance")) if col["balance"] is not None and cell(r, "balance") != "" else None
        except ValueError:
            bad.append(i + 1)
            continue
        low = narr.lower()
        if low in SUMMARY_WORDS and not cell(r, "vno"):
            if low == "opening":
                b["opening"] = round(dr - cr, 2)
                b["_run"] = b["opening"]
            elif low == "total":
                b["total_dr"], b["total_cr"] = dr, cr
            else:
                b["closing"] = round(dr - cr, 2)
            continue
        d = to_date(cell(r, "date"))
        if d is None:
            bad.append(i + 1)
            continue
        if dr and cr:
            checks.append(check("error", "BOOK_BOTH_SIDES",
                                f"Voucher {cell(r, 'vno')} (Excel row {i+1}) has both debit and credit."))
        vno = cell(r, "vno")
        vno = str(int(vno)) if isinstance(vno, float) else str(vno)
        instr = str(cell(r, "instr")).strip()
        instr_no = re.match(r"^\s*(\d{3,})", instr)
        idm = INSTR_DATE_RE.search(instr)
        instr_date = to_date(idm.group(1)) if idm else None
        nm = NAME_RE.search(narr)
        la = LOANAC_RE.search(narr)
        lt = LOANTYPE_RE.search(narr)
        b["sum_dr"] += dr
        b["sum_cr"] += cr
        b["count"] += 1
        if b["_run"] is not None and bal is not None:
            b["_run"] = round(b["_run"] + dr - cr, 2)
            if abs(b["_run"] - bal) > TOL:
                b["run_breaks"] += 1
                b["_run"] = bal
        rows.append({
            "row_no": i + 1,
            "branch": str(cell(r, "branch")),
            "date": d,
            "vno": vno,
            "vtype": str(cell(r, "vtype")),
            "accounts": str(cell(r, "accounts")),
            "instrument": instr,
            "instr_no": instr_no.group(1) if instr_no else "",
            "l4": instr_no.group(1)[-4:] if instr_no else "",
            "instr_date": instr_date.isoformat() if instr_date else "",
            "narration": narr,
            "notes": str(cell(r, "notes")),
            "receipt": dr,      # Debit in bank ledger = money into bank
            "payment": cr,      # Credit in bank ledger = money out of bank
            "balance": bal,
            "name": nm.group(1).strip() if nm else "",
            "loan_ac": la.group(1) if la else "",
            "loan_type": lt.group(1).strip() if lt else "",
            "remarks": str(cell(r, "remarks")),
        })

    if bad:
        checks.append(check("error", "BOOK_UNREADABLE_ROWS",
                            f"{len(bad)} row(s) could not be read (Excel rows {bad[:10]})."))

    # per-branch arithmetic — every branch in the ERP export carries Opening / Total / Closing rows
    have_any_summary = any(b["opening"] is not None or b["closing"] is not None for b in branches.values())
    opening = closing = 0.0
    branch_fail = False
    have_summary = False
    for key, b in branches.items():
        b["sum_dr"] = round(b["sum_dr"], 2)
        b["sum_cr"] = round(b["sum_cr"], 2)
        missing = [n for n, v in (("Opening", b["opening"]), ("Total", b["total_dr"]), ("Closing", b["closing"]))
                   if v is None]
        if missing and len(missing) < 3 or (len(missing) == 3 and b["count"] and have_any_summary):
            branch_fail = True
            checks.append(check("error", "BOOK_BRANCH_SUMMARY",
                                f"{b['branch']}: {', '.join(missing)} row(s) missing"
                                + (f" for {b['count']} entr(ies)" if b["count"] else "")
                                + ". The export may have been edited."))
            continue
        if len(missing) == 3:
            continue
        have_summary = True
        op = b["opening"] or 0.0
        cl = b["closing"] if b["closing"] is not None else round(op + b["sum_dr"] - b["sum_cr"], 2)
        opening += op
        closing += cl
        exp_close = round(op + b["sum_dr"] - b["sum_cr"], 2)
        if abs(exp_close - cl) > TOL:
            branch_fail = True
            checks.append(check("error", "BOOK_BRANCH_TOTAL",
                                f"{b['branch']}: opening {inr(op)} + receipts {inr(b['sum_dr'])} − payments "
                                f"{inr(b['sum_cr'])} = {inr(exp_close)}, but report shows closing {inr(cl)}."))
        if b["total_dr"] is not None:
            op_dr, op_cr = (op, 0.0) if op >= 0 else (0.0, -op)
            if abs(b["total_dr"] - (op_dr + b["sum_dr"])) > TOL or abs(b["total_cr"] - (op_cr + b["sum_cr"])) > TOL:
                branch_fail = True
                checks.append(check("error", "BOOK_BRANCH_TOTALROW",
                                    f"{b['branch']}: 'Total' row does not equal opening + entries. "
                                    "Rows may have been deleted or edited."))
        if b["run_breaks"]:
            checks.append(check("warning", "BOOK_RUNNING_BAL",
                                f"{b['branch']}: running balance column breaks at {b['run_breaks']} place(s)."))
        b.pop("_run", None)
    for b in branches.values():
        b.pop("_run", None)
    if not have_summary:
        opening = 0.0
        closing = round(sum(r["receipt"] - r["payment"] for r in rows), 2)
        checks.append(check("warning", "BOOK_NO_SUMMARY",
                            "No Opening/Closing rows found; book balance assumed to start at zero."))
    elif not branch_fail:
        active = [b for b in branches.values() if b["count"] or (b["opening"] or 0) or (b["closing"] or 0)]
        checks.append(check("ok", "BOOK_TOTALS",
                            f"Opening + receipts − payments = closing verified for {len(active)} active "
                            f"branch(es) out of {len(branches)} listed."))

    dates = [r["date"] for r in rows]
    if p_from is None:
        checks.append(check("warning", "BOOK_PERIOD", "Report period not found in title; taken from entry dates."))
        p_from = min(dates) if dates else None
        p_to = max(dates) if dates else None
    out_of_period = [r for r in rows if p_from and (r["date"] < p_from or r["date"] > p_to)]
    if out_of_period:
        checks.append(check("error", "BOOK_OUT_OF_PERIOD",
                             f"{len(out_of_period)} entr(ies) dated outside the report period."))

    dup = {}
    for r in rows:
        if r["vno"]:
            dup.setdefault((r["branch"], r["vno"], r["receipt"], r["payment"]), []).append(r)
    dups = [v for v in dup.values() if len(v) > 1]
    if dups:
        checks.append(check("warning", "BOOK_DUP_VOUCHER",
                            f"{len(dups)} voucher number(s) appear more than once with the same amount: "
                            + ", ".join(v[0]["vno"] for v in dups[:10])))

    return {
        "ok": not any(c["level"] == "error" for c in checks),
        "rows": rows,
        "checks": checks,
        "company": company,
        "title": title.strip(),
        "opening": round(opening, 2),
        "closing": round(closing, 2),
        "total_receipts": round(sum(r["receipt"] for r in rows), 2),
        "total_payments": round(sum(r["payment"] for r in rows), 2),
        "period_from": p_from,
        "period_to": p_to,
        "branches": list(branches.values()),
    }
