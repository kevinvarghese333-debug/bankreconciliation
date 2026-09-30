"""Matching engine.

Works on plain "items". An item is one line from either side:
    key        unique id (str)
    side       "bank" | "book"
    dir        "out" (money left the bank) | "in" (money came in)
    date       datetime.date
    amount     positive float
    name       counter-party name (payee / payer / borrower)
    ref        bank UTR / reference (bank side)
    cheque     cheque number (bank side)
    l4         last-4 of UPI ref typed in ERP (book side)
    instr_no   instrument number typed in ERP (book side)
    loan_ac    loan account number (book side)
    text       all searchable text on the line
    carried    True if this item is an open item from an earlier day

Rules: amounts must be equal to the paisa, direction must agree (bank debit
<-> book payment, bank credit <-> book receipt), and dates must be within the
window. Everything the engine is unsure about is marked "review", never
silently accepted.
"""
from __future__ import annotations

import re
from collections import defaultdict

from rapidfuzz import fuzz

TOL = 0.005
STRONG = 85
PARTIAL = 60      # payments: flag for review from 60%
PARTIAL_IN = 72   # collections: be stricter, payers are often relatives
GROUP_NAME = 85   # every member of a "same name" group must clearly be the same person
TITLES = re.compile(r"\b(MRS|MR|MS|SMT|SRI|SHRI|DR|MISS|KUM)\b\.?")


def clean_name(s: str) -> str:
    s = str(s or "").upper()
    s = TITLES.sub(" ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_score(a: str, b: str) -> float:
    a, b = clean_name(a), clean_name(b)
    if not a or not b:
        return 0.0
    s = max(fuzz.token_set_ratio(a, b), fuzz.WRatio(a, b))
    # bank narrations cut names at ~20 chars: "SHALEEPH KUMAR E K" vs "SHALEEPH KUMAR E K..."
    ca, cb = a.replace(" ", ""), b.replace(" ", "")
    short, long_ = sorted((ca, cb), key=len)
    if len(short) >= 5 and long_.startswith(short):
        s = max(s, 92)
    return float(s)


def _sig(s: str) -> list[str]:
    return [t for t in clean_name(s).split() if len(t) >= 3]


def given_names_agree(a: str, b: str) -> bool:
    """The leading name of one side must appear in the other (initials ignored).
    Stops 'SUNIL KUMAR' vs 'ANIL KUMAR' or 'MARY JACOB' vs 'MARY KRISHNAKUMAR' passing as strong."""
    ta, tb = _sig(a), _sig(b)
    if not ta or not tb:
        return False

    def close(x, y):
        return x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x))) or fuzz.ratio(x, y) >= 88

    if not (any(close(ta[0], y) for y in tb) or any(close(tb[0], x) for x in ta)):
        return False
    # both have a surname / second name: every word on the shorter side must appear on the other
    if len(ta) >= 2 and len(tb) >= 2:
        short, long_ = sorted((ta, tb), key=len)
        return all(any(close(x, y) for y in long_) for x in short)
    return True


def strong_name(a: str, b: str) -> float | None:
    s = name_score(a, b)
    return s if s >= STRONG and given_names_agree(a, b) else None


def same_amount(a: float, b: float) -> bool:
    return abs(a - b) <= TOL


def days_apart(a, b) -> int:
    return abs((a - b).days)


def find_subset(target: float, items: list[dict], max_size: int = 6, max_items: int = 24) -> list[dict] | None:
    """Smallest subset of items (size >= 2) whose amounts sum to target. Bounded search."""
    pool = sorted(items[:max_items], key=lambda x: -x["amount"])
    tgt = round(target * 100)
    amts = [round(i["amount"] * 100) for i in pool]
    n = len(pool)
    suffix = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = suffix[i + 1] + amts[i]
    best: list[int] | None = None
    budget = [50000]  # hard cap on search steps

    def dfs(start, remaining, chosen):
        nonlocal best
        budget[0] -= 1
        if budget[0] <= 0:
            return
        if remaining == 0 and len(chosen) >= 2:
            if best is None or len(chosen) < len(best):
                best = list(chosen)
            return
        if len(chosen) >= max_size or (best is not None and len(chosen) + 1 >= len(best)):
            return
        for i in range(start, n):
            if i > start and amts[i] == amts[i - 1]:
                continue  # identical amounts give identical sums — try each value once per level
            if amts[i] > remaining:
                continue
            if suffix[i] < remaining:
                return
            chosen.append(i)
            dfs(i + 1, remaining - amts[i], chosen)
            chosen.pop()

    if n >= 2 and sum(amts) >= tgt:
        dfs(0, tgt, [])
    return [pool[i] for i in best] if best else None


class Matcher:
    def __init__(self, bank: list[dict], book: list[dict], window_days: int = 7,
                 pool_collections: bool = True):
        self.bank = bank
        self.book = book
        self.window = window_days
        self.pool_collections = pool_collections
        self.used: set[str] = set()
        self.matches: list[dict] = []

    # ------------------------------------------------------------------ plumbing
    def placeholder_refs(self) -> set[str]:
        if getattr(self, "_placeholders", None) is None:
            names = defaultdict(set)
            for k in self.book:
                if k["dir"] == "in" and k.get("l4"):
                    names[(k["l4"], k["date"])].add(clean_name(k["name"]))
            bank_l4 = {(b.get("ref") or "")[-4:] for b in self.bank if b["dir"] == "in"}
            self._placeholders = {l4 for (l4, _), ns in names.items() if len(ns) >= 3 and l4 not in bank_l4}
        return self._placeholders

    def free(self, items, direction):
        return [i for i in items if i["dir"] == direction and i["key"] not in self.used]

    def add(self, bank_items, book_items, method, confidence, note=""):
        for i in (*bank_items, *book_items):
            self.used.add(i["key"])
        self.matches.append({
            "bank": [i["key"] for i in bank_items],
            "book": [i["key"] for i in book_items],
            "method": method,
            "confidence": confidence,
            "note": note,
        })

    def ok_dates(self, bi, ki):
        return days_apart(bi["date"], ki["date"]) <= self.window

    def pair_pass(self, direction, accept, method, confidence, note_fn=None):
        """Greedy one-to-one pass: best-scoring pairs first. A 'strong' pair is downgraded to
        'review' when another free candidate with the same amount scores almost as well."""
        banks = self.free(self.bank, direction)
        books = self.free(self.book, direction)
        by_amt = defaultdict(list)
        for k in books:
            by_amt[round(k["amount"], 2)].append(k)
        pairs = []
        for b in banks:
            for k in by_amt.get(round(b["amount"], 2), []):
                if not self.ok_dates(b, k):
                    continue
                sc = accept(b, k)
                if sc is not None:
                    pairs.append((sc, -days_apart(b["date"], k["date"]), b, k))
        pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
        for sc, _, b, k in pairs:
            if b["key"] in self.used or k["key"] in self.used:
                continue
            conf, note = confidence, (note_fn(b, k, sc) if note_fn else "")
            if confidence == "strong":
                rivals = [p for p in pairs if p[0] >= sc - 3 and (p[2] is b) != (p[3] is k)
                          and (p[2] is b or p[3] is k)
                          and p[2]["key"] not in self.used and p[3]["key"] not in self.used]
                if rivals:
                    other = rivals[0][3] if rivals[0][2] is b else rivals[0][2]
                    conf = "review"
                    note = (f"Ambiguous: '{other['name']}' ({other['side']}, {other['date']:%d-%m-%Y}) has the same "
                            f"amount and a similar name — confirm the right pair")
            self.add([b], [k], method if conf == confidence else method + " (ambiguous)", conf, note)

    # ------------------------------------------------------------------ passes
    def run(self) -> list[dict]:
        for direction in ("out", "in"):
            self.reference_pass(direction)
            if direction == "in":
                self.utr_l4_passes()
            self.pair_pass(direction, lambda b, k: strong_name(b["name"], k["name"]), "Amount + name", "strong")
            self.group_passes(direction)
            floor = PARTIAL if direction == "out" else PARTIAL_IN
            self.pair_pass(direction,
                           lambda b, k, floor=floor: s if (s := name_score(b["name"], k["name"])) >= floor else None,
                           "Amount + partial name", "review",
                           lambda b, k, s: f"Name similarity {s:.0f}% — bank '{b['name']}' vs book '{k['name']}'")
            if direction == "out":
                # Payments: a unique amount on the day is strong evidence, but the name must be checked.
                self.unique_amount_pass(direction)
            elif self.pool_collections:
                self.pooled_pass()
        return self.matches

    def pooled_pass(self):
        """Collections left over after all pairing rules. If, for a date, the unpaired bank
        credits and unpaired book receipts total exactly the same, record them as one pooled
        match (review). Payers are often relatives and branch staff mistype UPI refs, so a
        one-to-one pairing is not provable — but the money agrees to the paisa."""
        banks = defaultdict(list)
        books = defaultdict(list)
        for b in self.free(self.bank, "in"):
            if not b.get("carried"):
                banks[b["date"]].append(b)
        for k in self.free(self.book, "in"):
            if not k.get("carried"):
                books[k["date"]].append(k)
        for d, bl in banks.items():
            kl = books.get(d, [])
            if len(bl) >= 2 and len(kl) >= 2 and same_amount(sum(i["amount"] for i in bl), sum(i["amount"] for i in kl)):
                total = sum(i["amount"] for i in bl)
                self.add(bl, kl, "Collections pooled by day total", "review",
                         f"{len(bl)} bank credits and {len(kl)} book receipts on {d:%d-%m-%Y} could not be "
                         f"paired one-to-one, but both total {total:,.2f}")

    def reference_pass(self, direction):
        """Bank UTR / cheque number found as a whole number in the ERP entry (within the date window)."""
        banks = self.free(self.bank, direction)
        books = self.free(self.book, direction)
        for b in banks:
            refs = [r for r in (b.get("ref"), b.get("cheque")) if r and len(r) >= 6 and r.strip("0")]
            if not refs:
                continue
            for k in books:
                if k["key"] in self.used or not same_amount(b["amount"], k["amount"]) or not self.ok_dates(b, k):
                    continue
                hit = next((r for r in refs if r == k.get("instr_no")
                            or re.search(rf"(?<![0-9]){re.escape(r)}(?![0-9])", k["text"])), None)
                if hit:
                    ok_name = not (b["name"] and k["name"]) or name_score(b["name"], k["name"]) >= PARTIAL
                    self.add([b], [k], "Reference + amount", "strong" if ok_name else "review",
                             f"Ref {hit}" + ("" if ok_name else f" — but names differ: bank '{b['name']}' vs book '{k['name']}'"))
                    break

    def utr_l4_passes(self):
        """ERP 'Instrument.Det' holds the last 4 digits of the UPI reference."""
        banks = self.free(self.bank, "in")
        books = self.free(self.book, "in")
        # single
        for b in banks:
            l4 = (b.get("ref") or "")[-4:]
            if len(l4) < 4:
                continue
            cands = [k for k in books if k["key"] not in self.used and k.get("l4") == l4
                     and same_amount(b["amount"], k["amount"]) and self.ok_dates(b, k)]
            if cands:
                k = max(cands, key=lambda c: name_score(b["name"], c["name"]))
                sc = name_score(b["name"], k["name"])
                if sc >= PARTIAL_IN or len(cands) == 1 and sc >= 50:
                    self.add([b], [k], "UTR last-4 + amount", "strong", f"UTR …{l4}")
                else:
                    self.add([b], [k], "UTR last-4 + amount", "review",
                             f"UTR …{l4} agrees but payer '{b['name']}' vs borrower '{k['name']}' — likely a "
                             "relative paying; confirm")
        # group: one UPI paying several loans, all booked with the same last-4
        books = self.free(self.book, "in")
        groups = defaultdict(list)
        for k in books:
            if k.get("l4"):
                groups[(k["l4"], k["date"])].append(k)
        for b in self.free(self.bank, "in"):
            l4 = (b.get("ref") or "")[-4:]
            for (gl4, gdate), members in groups.items():
                if gl4 != l4 or days_apart(b["date"], gdate) > self.window:
                    continue
                members = [m for m in members if m["key"] not in self.used]
                if len(members) < 2:
                    continue
                if same_amount(sum(m["amount"] for m in members), b["amount"]):
                    self.add([b], members, "UTR last-4 + group total", "strong",
                             f"UTR …{l4} covers {len(members)} loan entries")
                    break
                sub = find_subset(b["amount"], members, max_size=len(members), max_items=16)
                if sub:
                    self.add([b], sub, "UTR last-4 + part of group", "strong",
                             f"UTR …{l4} covers {len(sub)} of {len(members)} entries with that last-4")
                    break

    def group_passes(self, direction):
        """One bank line = several book lines, and the reverse. Always 'review'."""
        # A) one bank line -> several book lines (same payer/reference, or same l4 in book)
        for b in sorted(self.free(self.bank, direction), key=lambda x: -x["amount"]):
            books = [k for k in self.free(self.book, direction) if self.ok_dates(b, k)
                     and k["amount"] < b["amount"] - TOL]
            related = [k for k in books if name_score(b["name"], k["name"]) >= GROUP_NAME]
            sub = find_subset(b["amount"], related) if len(related) >= 2 else None
            if sub:
                self.add([b], sub, "One bank line = several book entries (same name)", "review",
                         f"{len(sub)} book entries for '{b['name']}' add up to this bank line")
                continue
            # same l4 cluster in book (group leader pays for members). A last-4 typed against
            # 3+ different borrowers with no bank credit ending in it is a placeholder, not evidence.
            clusters = defaultdict(list)
            for k in books:
                if k.get("l4"):
                    clusters[k["l4"]].append(k)
            for l4, members in clusters.items():
                if len(members) < 2 or l4 in self.placeholder_refs():
                    continue
                sub = find_subset(b["amount"], members, max_size=len(members), max_items=16)
                if sub:
                    self.add([b], sub, "One bank line = several book entries (same ref in book)", "review",
                             f"{len(sub)} entries booked with ref …{l4} add up to this bank line; "
                             f"payer in bank: '{b['name']}'")
                    break
        # B) one book line -> several bank lines from the same party (paid in instalments)
        for k in sorted(self.free(self.book, direction), key=lambda x: -x["amount"]):
            banks = [b for b in self.free(self.bank, direction) if self.ok_dates(b, k)
                     and b["amount"] < k["amount"] - TOL and name_score(b["name"], k["name"]) >= STRONG]
            sub = find_subset(k["amount"], banks, max_size=4) if len(banks) >= 2 else None
            if sub:
                self.add(sub, [k], "Several bank lines = one book entry (same name)", "review",
                         f"{len(sub)} bank lines from '{k['name']}' add up to this book entry")

    def unique_amount_pass(self, direction):
        """Only one bank and one book line left with this amount (within the date window)."""
        banks = self.free(self.bank, direction)
        books = self.free(self.book, direction)
        bank_by = defaultdict(list)
        book_by = defaultdict(list)
        for b in banks:
            bank_by[round(b["amount"], 2)].append(b)
        for k in books:
            book_by[round(k["amount"], 2)].append(k)
        for amt, bl in bank_by.items():
            kl = book_by.get(amt, [])
            if len(bl) == 1 and len(kl) == 1 and self.ok_dates(bl[0], kl[0]) \
                    and not bl[0].get("carried") and not kl[0].get("carried"):
                b, k = bl[0], kl[0]
                same_day = b["date"] == k["date"]
                self.add([b], [k], "Amount only (unique " + ("on the day)" if same_day else "in window)"), "review",
                         f"Names differ — bank '{b['name'] or b['text'][:40]}' vs book "
                         f"'{k['name'] or k.get('narration', '')[:40]}'. Verify.")


# ---------------------------------------------------------------------------- flags

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "info": 3}


def build_flags(bank: list[dict], book: list[dict], matches: list[dict], account_type: str,
                as_of, aged_days: int = 3) -> list[dict]:
    from .parsers import inr

    flags: list[dict] = []
    matched = {k for m in matches for k in (*m["bank"], *m["book"])}
    byk = {i["key"]: i for i in (*bank, *book)}

    def f(sev, code, title, detail, keys=()):
        flags.append({"severity": sev, "code": code, "title": title, "detail": detail, "keys": list(keys)})

    # unmatched lines
    for b in bank:
        if b["key"] in matched:
            continue
        if b["dir"] == "out":
            what = "Outflow from collection account" if account_type == "collection" else "Bank debit"
            f("critical", "BANK_DEBIT_NOT_IN_BOOK", f"{what} not recorded in bank book",
              f"{b['date']:%d-%m-%Y} · ₹{inr(b['amount'])} · {b['name'] or b['text'][:60]}", [b["key"]])
        else:
            f("medium", "BANK_CREDIT_NOT_IN_BOOK", "Credit in bank not posted in bank book",
              f"{b['date']:%d-%m-%Y} · ₹{inr(b['amount'])} · {b['name']}", [b["key"]])
    for k in book:
        if k["key"] in matched:
            continue
        age = (as_of - k["date"]).days
        aged = f" · open {age} day(s)" if age else ""
        if k["dir"] == "out":
            f("critical" if age >= aged_days else "high", "BOOK_PAYMENT_NOT_IN_BANK",
              "Payment in bank book not debited by bank" + (" (aged)" if age >= aged_days else ""),
              f"Vchr {k['vno']} · {k['date']:%d-%m-%Y} · ₹{inr(k['amount'])} · {k['name']}{aged}", [k["key"]])
        else:
            f("critical" if age >= aged_days else "high", "BOOK_RECEIPT_NOT_IN_BANK",
              "Receipt in bank book not credited by bank" + (" (aged)" if age >= aged_days else ""),
              f"Vchr {k['vno']} · {k['date']:%d-%m-%Y} · ₹{inr(k['amount'])} · {k['name']}{aged}", [k["key"]])

    # weak matches: on payments money may have gone to the wrong person
    soft_in = []
    for m in matches:
        if m["confidence"] != "review":
            continue
        items = [byk[x] for x in (*m["bank"], *m["book"]) if x in byk]
        if not items:
            continue
        direction = items[0]["dir"]
        amt = sum(byk[x]["amount"] for x in m["bank"] if x in byk)
        if m["method"].startswith("Collections pooled"):
            f("medium", "POOLED_COLLECTIONS", "Collections agree only on the day total",
              f"₹{inr(amt)} · {m['note']}. Open the Collections tab to review them.",
              [*m["bank"], *m["book"]])
            continue
        sev = "medium" if direction == "out" else "info"
        if direction == "out" and m["method"].startswith("Amount only"):
            sev = "high"
        if "(ambiguous)" in m["method"]:
            sev = "high" if direction == "out" else "medium"
        if direction == "in" and sev == "info":
            soft_in.append((m, amt))
            continue
        title = ("Payment matched but beneficiary name differs — verify" if direction == "out"
                 else "Collection matched on partial evidence — verify")
        f(sev, "REVIEW_MATCH", title, f"₹{inr(amt)} · {m['method']} · {m['note']}", [*m["bank"], *m["book"]])
    if soft_in:
        kinds = defaultdict(int)
        for m, _ in soft_in:
            kinds[m["method"]] += 1
        f("info", "REVIEW_COLLECTIONS", f"{len(soft_in)} collection(s) matched on partial evidence",
          f"₹{inr(sum(a for _, a in soft_in))} in total — "
          + "; ".join(f"{n} × {k}" for k, n in sorted(kinds.items(), key=lambda x: -x[1]))
          + ". Usually a relative paying for the borrower. Spot-check in the Collections tab → 'Only items to verify'.",
          [x for m, _ in soft_in for x in (*m["bank"], *m["book"])])

    # collection account: every outflow is listed
    if account_type == "collection":
        outs = [b for b in bank if b["dir"] == "out" and not b.get("carried")]
        if len(outs) > 1:
            f("medium", "COLLECTION_MULTI_OUT", "More than one outflow from collection account",
              f"{len(outs)} debits today totalling ₹{inr(sum(b['amount'] for b in outs))}. "
              "Only the day-end sweep is expected.", [b["key"] for b in outs])
        for b in outs:
            if b["key"] in matched:
                f("info", "COLLECTION_OUT", "Outflow from collection account (recorded in book)",
                  f"₹{inr(b['amount'])} · {b['text'][:70]}", [b["key"]])
    else:
        non_api = [b for b in bank if b["dir"] == "out" and not b.get("carried") and b.get("channel") != "RAP"]
        if non_api:
            f("info", "NON_API_DEBIT", f"{len(non_api)} debit(s) without the RAP (bank API) prefix",
              "Usually a truncated bank narration; check they were genuine API disbursals: "
              + ", ".join(f"{b['name']} ₹{inr(b['amount'])}" for b in non_api[:8]),
              [b["key"] for b in non_api])

    # duplicate disbursal checks
    by_loan = defaultdict(list)
    for k in book:
        if k["dir"] == "out" and k.get("loan_ac"):
            by_loan[k["loan_ac"]].append(k)
    for loan, ks in by_loan.items():
        if len(ks) > 1:
            f("high", "DUP_LOAN_PAYMENT", "Same loan account paid more than once",
              f"Loan A/c {loan} · {len(ks)} payments: " + ", ".join(f"Vchr {k['vno']} ₹{inr(k['amount'])}" for k in ks),
              [k["key"] for k in ks])
    by_ben = defaultdict(list)
    for b in bank:
        if b["dir"] == "out" and not b.get("carried"):
            by_ben[(clean_name(b["name"]), round(b["amount"], 2))].append(b)
    for (nm, amt), bs in by_ben.items():
        if nm and len(bs) > 1:
            f("high", "DUP_BANK_PAYMENT", "Same beneficiary and amount debited more than once",
              f"{nm} · ₹{inr(amt)} × {len(bs)}", [b["key"] for b in bs])

    # data-entry quality in ERP instrument field
    by_l4 = defaultdict(set)
    for k in book:
        if k["dir"] == "in" and k.get("l4") and not k.get("carried"):
            by_l4[(k["l4"], k["date"])].add(clean_name(k["name"]))
    bank_l4 = {(b.get("ref") or "")[-4:] for b in bank if b["dir"] == "in"}
    for (l4, d), names in by_l4.items():
        if len(names) >= 3 and l4 not in bank_l4:
            f("info", "DATA_ENTRY_REF", f"UPI ref '…{l4}' typed against {len(names)} different borrowers",
              "No bank credit ends in these digits — looks like a placeholder. Borrowers: "
              + ", ".join(sorted(names)[:8]), [k["key"] for k in book if k.get("l4") == l4 and k["date"] == d])
    no_ref = [k for k in book if k["dir"] == "in" and not k.get("l4") and not k.get("carried")]
    if no_ref:
        f("info", "DATA_ENTRY_NOREF", f"{len(no_ref)} receipt(s) booked without a UPI reference",
          ", ".join(k["name"] for k in no_ref[:10]), [k["key"] for k in no_ref])

    flags.sort(key=lambda x: SEV_ORDER[x["severity"]])
    return flags


# ---------------------------------------------------------------------------- BRS

def compute_brs(book_closing: float, bank_closing: float, open_bank: list[dict], open_book: list[dict],
                opening_diff: float = 0.0) -> dict:
    """Bank Reconciliation Statement, starting from balance as per bank book.

    Balance as per bank book
      + Payments entered in book, not yet debited by bank
      − Receipts entered in book, not yet credited by bank
      + Credits in bank statement, not entered in book
      − Debits in bank statement, not entered in book
      ± Opening difference carried from first reconciliation (not itemised)
      = Balance as per bank statement
    """
    a = round(sum(i["amount"] for i in open_book if i["dir"] == "out"), 2)
    b = round(sum(i["amount"] for i in open_book if i["dir"] == "in"), 2)
    c = round(sum(i["amount"] for i in open_bank if i["dir"] == "in"), 2)
    d = round(sum(i["amount"] for i in open_bank if i["dir"] == "out"), 2)
    computed = round(book_closing + a - b + c - d + opening_diff, 2)
    diff = round(bank_closing - computed, 2)
    return {
        "book_closing": book_closing,
        "add_book_payments_not_debited": a,
        "less_book_receipts_not_credited": b,
        "add_bank_credits_not_in_book": c,
        "less_bank_debits_not_in_book": d,
        "opening_diff": opening_diff,
        "computed_bank_balance": computed,
        "bank_closing": bank_closing,
        "difference": diff,
        "tallies": abs(diff) <= TOL,
        # reconciled without leaning on an unexplained, unitemised difference
        "clean": abs(diff) <= TOL and abs(opening_diff) <= TOL,
        "counts": {
            "a": sum(1 for i in open_book if i["dir"] == "out"),
            "b": sum(1 for i in open_book if i["dir"] == "in"),
            "c": sum(1 for i in open_bank if i["dir"] == "in"),
            "d": sum(1 for i in open_bank if i["dir"] == "out"),
        },
    }
