"""End-to-end tests. Run:  python -m pytest -q   (from the project folder)"""
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="bankreco-test-")
os.environ["DATA_DIR"] = TMP

from app import db, engine  # noqa: E402
from tests.synth import write_book, write_statement  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture()
def conn():
    shutil.rmtree(TMP, ignore_errors=True)
    db.init()
    c = db.connect()
    c.execute("INSERT INTO accounts(bank,acc_no,nickname,kind,created_at) VALUES('SIB','1113','Main','main',?)", (db.now(),))
    c.execute("INSERT INTO accounts(bank,acc_no,nickname,kind,created_at) VALUES('SIB','2201','Coll','collection',?)", (db.now(),))
    yield c
    c.close()


def run(conn, acc, bank, book, **kw):
    """Reconcile; a first run's mandatory account confirmation is acknowledged automatically."""
    try:
        return engine.reconcile(conn, acc, str(bank), Path(bank).name, str(book), Path(book).name, "test", **kw)
    except engine.Blocked as e:
        if e.ackable and {c["code"] for c in e.ackable} == {"FIRST_RUN"} and not kw.get("acknowledged"):
            return engine.reconcile(conn, acc, str(bank), Path(bank).name, str(book), Path(book).name, "test",
                                    acknowledged=True, ack_reason="Confirmed account for first run")
        raise


def brs(conn, rid):
    return json.loads(conn.execute("SELECT brs_json FROM runs WHERE id=?", (rid,)).fetchone()[0])


def flags(conn, rid):
    return json.loads(conn.execute("SELECT flags_json FROM runs WHERE id=?", (rid,)).fetchone()[0])


def codes(conn, rid):
    return [f["code"] for f in flags(conn, rid)]


def test_real_sample_tallies(conn):
    if not (SAMPLES / "SIB_1113_26-09-2026.xls").exists():
        pytest.skip("sample files not present")
    rid = run(conn, 1, SAMPLES / "SIB_1113_26-09-2026.xls", SAMPLES / "BankBook_1113_26-09-2026.xlsx")
    b = brs(conn, rid)
    assert b["tallies"] and b["difference"] == 0
    s = json.loads(conn.execute("SELECT summary_json FROM runs WHERE id=?", (rid,)).fetchone()[0])
    assert s["bank_out"]["matched"] == s["bank_out"]["count"] == 109
    assert "BANK_DEBIT_NOT_IN_BOOK" not in codes(conn, rid)


def test_carry_forward_and_exceptions(conn, tmp_path):
    d1, d2 = date(2026, 9, 28), date(2026, 9, 29)
    op = 1_000_000.00
    # Day 1: A paid & debited; B booked but bank debits tomorrow; bank charge not booked; UPI collection
    bank1 = tmp_path / "b1.xlsx"
    bclose1 = write_statement(bank1, d1, op, [
        ("RAP/626900000001/ 0000095001 /ANITHA KUMARI/", 50000, 0),
        ("UPI/SBIN/626911112222/RAJI BIJEESH/UPI/x/raji@ok", 0, 800),
        ("CHRG/SMS CHARGES Q3", 59, 0),
    ])
    book1 = tmp_path / "k1.xlsx"
    kclose1 = write_book(book1, d1, op, [
        dict(vno=1, name="ANITHA KUMARI", loan_ac="0001", payment=50000),
        dict(vno=2, name="BINDU RAJAN", loan_ac="0002", payment=30000),
        dict(vno=3, name="RAJI BIJEESH", loan_ac="0003", instr="2222", receipt=800),
    ])
    r1 = run(conn, 1, bank1, book1)
    b = brs(conn, r1)
    assert b["tallies"], b
    assert b["add_book_payments_not_debited"] == 30000
    assert b["less_bank_debits_not_in_book"] == 59
    c = codes(conn, r1)
    assert "BANK_DEBIT_NOT_IN_BOOK" in c and "BOOK_PAYMENT_NOT_IN_BANK" in c

    # Day 2: B debited by bank (clears carry); charge booked (clears carry); fake receipt in book only
    bank2 = tmp_path / "b2.xlsx"
    write_statement(bank2, d2, bclose1, [("RAP/626900000009/ 0000095002 /BINDU RAJAN/", 30000, 0)])
    book2 = tmp_path / "k2.xlsx"
    write_book(book2, d2, kclose1, [
        dict(vno=4, name="", payment=59, vtype="JV", narration="Bank charges SMS Q3"),
        dict(vno=5, name="SUMA K", loan_ac="0005", instr="9999", receipt=1000),
    ])
    r2 = run(conn, 1, bank2, book2)
    b = brs(conn, r2)
    assert b["tallies"], b
    # BINDU's ₹30,000 cleared. The ₹59 charge booked a day late has no name/ref, so it is NOT
    # auto-cleared against the carried bank line — both sides stay open until matched by hand.
    assert b["add_book_payments_not_debited"] == 59
    assert b["less_bank_debits_not_in_book"] == 59
    open_ = engine.open_items_as_of(conn, 1, r2)
    ids = [i["id"] for i in open_ if i["amount"] == 59]
    engine.manual_match(conn, 1, ids, "Bank SMS charges booked next day", "test")
    b = brs(conn, r2)
    assert b["tallies"] and b["less_bank_debits_not_in_book"] == 0 and b["add_book_payments_not_debited"] == 0
    assert b["less_book_receipts_not_credited"] == 1000
    c = codes(conn, r2)
    assert "BOOK_RECEIPT_NOT_IN_BANK" in c
    assert "BANK_DEBIT_NOT_IN_BOOK" not in c
    s = json.loads(conn.execute("SELECT summary_json FROM runs WHERE id=?", (r2,)).fetchone()[0])
    assert s["carried_cleared"] >= 1

    # Undo day 2 -> day-1 items reopen
    engine.undo_run(conn, r2, "test")
    assert brs(conn, r1)["add_book_payments_not_debited"] == 30000


def test_tampered_statement_blocked(conn, tmp_path):
    import openpyxl
    bank = tmp_path / "b.xlsx"
    write_statement(bank, date(2026, 9, 28), 1000.0,
                    [("RAP/1/ 1 /A/", 100, 0), ("RAP/2/ 2 /X/", 50, 0), ("RAP/3/ 3 /B/", 200, 0)])
    wb = openpyxl.load_workbook(bank)
    wb.active.delete_rows(3)  # quietly remove the middle transaction
    wb.save(bank)
    book = tmp_path / "k.xlsx"
    write_book(book, date(2026, 9, 28), 1000.0, [dict(vno=1, name="B", payment=200)])
    with pytest.raises(engine.Blocked) as e:
        run(conn, 1, bank, book)
    assert any(c["code"] == "BANK_BALANCE_BREAK" and c["level"] == "error" for c in e.value.checks)
    assert e.value.ackable == []  # cannot be overridden


def test_continuity_break_needs_ack(conn, tmp_path):
    d1, d2 = date(2026, 9, 28), date(2026, 9, 30)
    b1, k1 = tmp_path / "b1.xlsx", tmp_path / "k1.xlsx"
    write_statement(b1, d1, 5000, [("RAP/1/ 1 /A/", 100, 0)])
    write_book(k1, d1, 5000, [dict(vno=1, name="A", payment=100)])
    run(conn, 1, b1, k1)
    b2, k2 = tmp_path / "b2.xlsx", tmp_path / "k2.xlsx"
    write_statement(b2, d2, 4800, [("RAP/2/ 2 /C/", 50, 0)])   # 100 missing vs previous closing 4900
    write_book(k2, d2, 4900, [dict(vno=2, name="C", payment=50)])
    with pytest.raises(engine.Blocked) as e:
        run(conn, 1, b2, k2, acknowledged=True, ack_reason="Missing statement for 28th, checked with bank")
    assert any(c["code"] == "BANK_CONTINUITY" and c["level"] == "error" for c in e.value.checks)
    assert e.value.ackable == []   # a broken bank chain can never be waved through


def test_book_backdated_entry_needs_reason_and_stays_critical(conn, tmp_path):
    d1, d2 = date(2026, 9, 28), date(2026, 9, 29)
    b1, k1 = tmp_path / "b1.xlsx", tmp_path / "k1.xlsx"
    bc = write_statement(b1, d1, 5000, [("RAP/1/ 1 /ANU/", 100, 0)])
    kc = write_book(k1, d1, 5000, [dict(vno=1, name="ANU", payment=100)])
    run(conn, 1, b1, k1)
    b2, k2 = tmp_path / "b2.xlsx", tmp_path / "k2.xlsx"
    write_statement(b2, d2, bc, [("RAP/2/ 2 /CHITRA/", 50, 0)])
    write_book(k2, d2, kc - 700, [dict(vno=2, name="CHITRA", payment=50)])   # back-dated ₹700 entry in ERP
    with pytest.raises(engine.Blocked) as e:
        run(conn, 1, b2, k2)
    assert "BOOK_CONTINUITY" in {c["code"] for c in e.value.ackable}
    with pytest.raises(engine.Blocked):
        run(conn, 1, b2, k2, acknowledged=True, ack_reason=".")          # too short
    rid = run(conn, 1, b2, k2, acknowledged=True, ack_reason="Back-dated JV 28-09 for ₹700, to be traced")
    b = brs(conn, rid)
    assert b["tallies"] and not b["clean"] and b["opening_diff"] == 700
    assert codes(conn, rid)[0] == "UNEXPLAINED_DIFFERENCE"
    assert flags(conn, rid)[0]["severity"] == "critical"


def test_collection_account_outflows(conn, tmp_path):
    d = date(2026, 9, 28)
    b, k = tmp_path / "b.xlsx", tmp_path / "k.xlsx"
    write_statement(b, d, 0, [
        ("UPI/SBIN/626900001111/SHEEBA JOY/UPI/x/s@ok", 0, 800),
        ("UPI/SBIN/626900002222/NIMISHA/UPI/x/n@ok", 0, 700),
        ("NEFT/SIBLN00001/UNKNOWN PARTY/", 1000, 0),
        ("TRF/SWEEP TO 1113", 500, 0),
    ])
    write_book(k, d, 0, [
        dict(vno=1, name="SHEEBA JOY", instr="1111", receipt=800),
        dict(vno=2, name="NIMISHA BAIJU", instr="2222", receipt=700),
        dict(vno=3, name="", payment=500, vtype="CT", narration="Sweep transfer to SIB 1113"),
    ])
    rid = run(conn, 2, b, k, acknowledged=True, ack_reason="First run for collection account")
    f = flags(conn, rid)
    crit = [x for x in f if x["severity"] == "critical"]
    assert len(crit) == 1 and crit[0]["code"] == "BANK_DEBIT_NOT_IN_BOOK"
    assert "COLLECTION_MULTI_OUT" in [x["code"] for x in f]
    assert brs(conn, rid)["tallies"]


def test_manual_match_and_unmatch(conn, tmp_path):
    d = date(2026, 9, 28)
    b, k = tmp_path / "b.xlsx", tmp_path / "k.xlsx"
    write_statement(b, d, 1000, [("RAP/1/ 1 /TOTALLY DIFFERENT/", 100, 0), ("RAP/2/ 2 /OTHER/", 100, 0)])
    write_book(k, d, 1000, [dict(vno=1, name="PERSON A", payment=100), dict(vno=2, name="PERSON B", payment=100)])
    rid = run(conn, 1, b, k)
    open_ = engine.open_items_as_of(conn, 1, rid)
    assert len(open_) == 4
    bank_id = next(i["id"] for i in open_ if i["side"] == "bank")
    book_id = next(i["id"] for i in open_ if i["side"] == "book")
    with pytest.raises(ValueError):
        engine.manual_match(conn, 1, [bank_id, book_id], "", "test")
    mid = engine.manual_match(conn, 1, [bank_id, book_id], "Confirmed with branch", "test")
    assert len(engine.open_items_as_of(conn, 1, rid)) == 2
    engine.unmatch(conn, mid, "test", "wrong pair")
    assert len(engine.open_items_as_of(conn, 1, rid)) == 4
    assert brs(conn, rid)["tallies"]


def test_duplicate_payment_flag(conn, tmp_path):
    d = date(2026, 9, 28)
    b, k = tmp_path / "b.xlsx", tmp_path / "k.xlsx"
    write_statement(b, d, 100000, [("RAP/1/ 1 /LILLY JOHNSON/", 15188, 0), ("RAP/2/ 2 /LILLY JOHNSON/", 15188, 0)])
    write_book(k, d, 100000, [dict(vno=1, name="LILLY JOHNSON", loan_ac="0034", payment=15188),
                              dict(vno=2, name="LILLY JOHNSON", loan_ac="0034", payment=15188)])
    rid = run(conn, 1, b, k)
    c = codes(conn, rid)
    assert "DUP_LOAN_PAYMENT" in c and "DUP_BANK_PAYMENT" in c


def _day(conn, tmp_path, tag, d, op, bank_rows, book_rows, acc=1, book_op=None, **kw):
    b, k = tmp_path / f"b{tag}.xlsx", tmp_path / f"k{tag}.xlsx"
    bc = write_statement(b, d, op, bank_rows)
    kc = write_book(k, d, op if book_op is None else book_op, book_rows)
    return run(conn, acc, b, k, **kw), bc, kc


def _conf(conn, rid, name):
    """confidence of the match that contains the book entry for `name`"""
    for i in engine.run_items(conn, rid):
        if i["side"] == "book" and i["name"] == name:
            if not i["match_id"]:
                return None
            return conn.execute("SELECT confidence FROM matches WHERE id=?", (i["match_id"],)).fetchone()[0]


def test_different_person_same_amount_is_not_strong(conn, tmp_path):
    rid, _, _ = _day(conn, tmp_path, 1, date(2026, 9, 28), 100000,
                     [("RAP/626900000001/ 0000095001 /SUNIL KUMAR/", 50000, 0)],
                     [dict(vno=1, name="ANIL KUMAR", loan_ac="0001", payment=50000)])
    assert _conf(conn, rid, "ANIL KUMAR") in (None, "review")


def test_tied_candidates_are_flagged_ambiguous(conn, tmp_path):
    rid, _, _ = _day(conn, tmp_path, 1, date(2026, 9, 28), 100000,
                     [("RAP/626900000001/ 0000095001 /THOMAS/", 20000, 0)],
                     [dict(vno=1, name="THOMAS VARGHESE", loan_ac="0001", payment=20000),
                      dict(vno=2, name="THOMAS K J", loan_ac="0002", payment=20000)])
    confs = {_conf(conn, rid, "THOMAS VARGHESE"), _conf(conn, rid, "THOMAS K J")}
    assert "strong" not in confs


def test_reference_fragment_does_not_clear(conn, tmp_path):
    d1 = date(2026, 9, 28)
    r1, bc, kc = _day(conn, tmp_path, 1, d1, 100000, [("RAP/1/ 1 /X/", 1, 0)],
                      [dict(vno=1, name="X", payment=1), dict(vno=2, name="BINDU", loan_ac="00030400600000000700", payment=25000)])
    b2, k2 = tmp_path / "b2.xlsx", tmp_path / "k2.xlsx"
    write_statement(b2, date(2026, 9, 29), bc, [("CLG/000700/UNKNOWN PERSON/", 25000, 0)])
    write_book(k2, date(2026, 9, 29), kc, [])
    r2 = run(conn, 1, b2, k2)
    assert _conf(conn, r2, "BINDU") in (None, "review")


def test_carried_payment_not_cleared_by_unrelated_debit(conn, tmp_path):
    r1, bc, kc = _day(conn, tmp_path, 1, date(2026, 9, 28), 100000, [("RAP/1/ 1 /X/", 1, 0)],
                      [dict(vno=1, name="X", payment=1), dict(vno=2, name="BINDU RAJAN", payment=30000)])
    b2, k2 = tmp_path / "b2.xlsx", tmp_path / "k2.xlsx"
    write_statement(b2, date(2026, 9, 29), bc, [("RAP/2/ 2 /SURESH MENON/", 30000, 0),
                                                 ("UPI/SBIN/626900007777/TOM/UPI/x/p@ok", 0, 250)])
    write_book(k2, date(2026, 9, 29), kc, [dict(vno=3, name="TOM", loan_ac="07", instr="7777", receipt=250)])
    r2 = run(conn, 1, b2, k2)
    assert _conf(conn, r2, "BINDU RAJAN") is None
    assert "BANK_DEBIT_NOT_IN_BOOK" in codes(conn, r2)


def test_double_disbursal_across_days(conn, tmp_path):
    r1, bc, kc = _day(conn, tmp_path, 1, date(2026, 9, 28), 100000, [("RAP/1/ 1 /LILLY JOHNSON/", 15188, 0)],
                      [dict(vno=1, name="LILLY JOHNSON", loan_ac="0034", payment=15188)])
    b2, k2 = tmp_path / "b2.xlsx", tmp_path / "k2.xlsx"
    write_statement(b2, date(2026, 9, 29), bc, [("RAP/2/ 2 /LILLY JOHNSON/", 15188, 0)])
    write_book(k2, date(2026, 9, 29), kc, [dict(vno=2, name="LILLY JOHNSON", loan_ac="0034", payment=15188)])
    r2 = run(conn, 1, b2, k2)
    c = codes(conn, r2)
    assert "DUP_LOAN_PAYMENT_HISTORY" in c and "DUP_BANK_PAYMENT_HISTORY" in c


def test_single_fake_receipt_not_pooled(conn, tmp_path):
    rid, _, _ = _day(conn, tmp_path, 1, date(2026, 9, 28), 1000,
                     [("UPI/SBIN/626900001111/ABDUL RAHIM/UPI/x/a@ok", 0, 1000)],
                     [dict(vno=1, name="SUMA K", instr="4321", receipt=1000)])
    assert "BOOK_RECEIPT_NOT_IN_BANK" in codes(conn, rid)


def test_branch_without_totals_rejected(tmp_path):
    import openpyxl
    from app.parsers import parse_bank_book
    k = tmp_path / "k.xlsx"
    write_book(k, date(2026, 9, 28), 1000, [dict(vno=1, name="A", payment=100)])
    wb = openpyxl.load_workbook(k)
    wb.active.append([9, "Ghost Branch", None, date(2026, 9, 28), 777, "BP", "x", None,
                      "Direct Pay, A/c: PERS, A/c No: 1, Name: SURESH", "", 0, 40000, None, ""])
    wb.save(k)
    assert not parse_bank_book(k)["ok"]


def test_newest_first_statement_uses_dates(tmp_path):
    import openpyxl
    from app.parsers import parse_bank_statement
    p = tmp_path / "s.xlsx"
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SlNo", "Transaction Date", "Value Date", "Particulars", None, "Cheque Number", "Withdrawals", None, "Deposits", "Balance Amount"])
    ws.append([2, "02-09-2026", "02-09-2026", "UPI/X/1/A/", None, None, None, None, "100.00", "1,100.00"])
    ws.append([1, "01-09-2026", "01-09-2026", "RAP/1/ 1 /B/", None, None, "100.00", None, None, "1,000.00"])
    wb.save(p)
    s = parse_bank_statement(p)
    assert s["ok"] and s["opening"] == 1100.0 and s["closing"] == 1100.0


def test_web_security(conn):
    from fastapi.testclient import TestClient
    from app.main import app, safe_back
    assert safe_back("//evil.com") == "/" and safe_back("/\\evil.com") == "/" and safe_back("/runs/1") == "/runs/1"
    c = TestClient(app, follow_redirects=False)
    r = c.get("/setup"); import re
    tok = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = c.post("/setup", data={"username": "kev", "password": "abcdefghijk1", "password2": "abcdefghijk1", "csrf_token": tok})
    assert c.get("/").status_code == 200
    stolen = dict(c.cookies)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/").text).group(1)
    c.post("/logout", data={"csrf_token": tok})
    thief = TestClient(app, follow_redirects=False, cookies=stolen)
    assert thief.get("/").status_code == 303          # old cookie no longer works
    anon = TestClient(app, follow_redirects=False)
    assert anon.post("/reconcile/cancel", data={"token": "abc"}).status_code == 303
    assert anon.get("/runs/1").status_code == 303


def test_accounts_bulk_import(conn, tmp_path):
    import openpyxl
    from app.accounts_import import parse_accounts, apply_import
    p = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append([None, "Aloor", "   SOUTH INDIAN BANK A/C NO 0058073000001317"])
    ws.append([None, "North Paravur", "SOUTH INDIAN BANK A/C NO 0058073000001113"])
    ws.append([None, "Head Office", "SOUTH INDIAN BANK A/C NO 0058073000001124"])
    ws.append([None, "Head Office", "SOUTH INDIAN BANK A/C NO 00580730001195"])
    wb.save(p)
    rows, problems = parse_accounts(str(p), main_last4={"1113"})
    assert len(rows) == 4 and {r["kind"] for r in rows if r["acc_no"].endswith("1113")} == {"main"}
    assert any("14 digits" in x for x in problems)
    r1 = apply_import(conn, rows, "t")
    assert r1["added"] == 4
    r2 = apply_import(conn, rows, "t")
    assert r2["added"] == 0 and r2["unchanged"] == 4


def test_new_narrations_and_sweeps(conn, tmp_path):
    """29-09-2026 SIB statement: sweeps, cash, RTGS, IMPS, IB all match a book booked by branch name."""
    src = SAMPLES / "SIB_1113_29-09-2026.xls"
    if not src.exists():
        pytest.skip("sample not present")
    from app.parsers import parse_bank_statement
    st = parse_bank_statement(src)
    branches = {"KALADY": "Kaladi", "AZHIKODE": "Azhicode", "KUTHIATHODE": "Kuthiyathode",
                "EAST KADUNGALLUR ALUVA": "East Kadungallur", "MUTTUCHIRA KADUTHURUTHY": "Muttuchira"}
    entries = []
    for n, r in enumerate(st["rows"], 1):
        if r["channel"] == "SWEEP":
            br = branches.get(r["branch"], r["branch"].title())
            entries.append(dict(vno=n, vtype="CT", name="", receipt=r["deposit"],
                                narration=f"Transfer from {br} collection account", ))
        elif r["deposit"]:
            entries.append(dict(vno=n, name=r["name"], receipt=r["deposit"]))
        else:
            entries.append(dict(vno=n, name=r["name"], payment=r["withdrawal"]))
    k = tmp_path / "k.xlsx"
    write_book(k, st["period_from"], st["opening"], entries)
    rid = run(conn, 1, src, k)
    b = brs(conn, rid)
    assert b["tallies"] and b["counts"] == {"a": 0, "b": 0, "c": 0, "d": 0}, b
    methods = [m["method"] for m in conn.execute("SELECT method FROM matches WHERE run_id=?", (rid,))]
    assert sum(m.startswith("Sweep from branch") for m in methods) == 33
    assert "RAP_RETURNED" in codes(conn, rid)


def _upi(ref, name, amt):
    return (f"UPI/SBIN/{ref}/{name}/UPI/x/p@ok", 0, amt)


def test_sunday_receipts_booked_on_monday(conn, tmp_path):
    sat, sun, mon = date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)
    r1, bc, kc = _day(conn, tmp_path, 1, sat, 100000,
                      [_upi("626900001234", "ANU", 500)],
                      [dict(vno=1, name="ANU", loan_ac="01", instr="1234", receipt=500)])
    # Sunday: three UPIs reach the bank; branches closed, ERP bank book has no entries
    r2, bc, kc2 = _day(conn, tmp_path, 2, sun, bc,
                       [_upi("626900001111", "ASHA MOL", 800), _upi("626900002222", "BINDU RAJ", 500),
                        _upi("626900003333", "CHINNU K", 700)], [], book_op=kc)
    assert kc2 == kc
    b = brs(conn, r2)
    assert b["tallies"] and b["c_holiday"] == 2000 and b["c_holiday_count"] == 3
    c = codes(conn, r2)
    assert "HOLIDAY_RECEIPTS_PENDING" in c and "BANK_CREDIT_NOT_IN_BOOK" not in c
    assert not [f for f in flags(conn, r2) if f["severity"] == "critical"]
    s = json.loads(conn.execute("SELECT summary_json FROM runs WHERE id=?", (r2,)).fetchone()[0])
    assert s["holidays"] == ["2026-09-27"]
    # Monday: ASHA booked with the Sunday instrument date, BINDU with Monday's date, CHINNU not booked
    r3, _, _ = _day(conn, tmp_path, 3, mon, bc, [_upi("626900004444", "DEVI S", 900)], book_op=kc, book_rows=[
        dict(vno=2, name="DEVI S", loan_ac="04", instr="4444", receipt=900),
        dict(vno=3, name="ASHA MOL", loan_ac="02", instr="1111", instr_date=sun, receipt=800),
        dict(vno=4, name="BINDU RAJ", loan_ac="03", instr="2222", receipt=500),
    ])
    b = brs(conn, r3)
    assert b["tallies"] and b["add_bank_credits_not_in_book"] == 700
    fl = flags(conn, r3)
    crit = [f for f in fl if f["code"] == "HOLIDAY_RECEIPT_NOT_BOOKED"]
    assert len(crit) == 1 and crit[0]["severity"] == "critical"
    assert "27-09-2026" in crit[0]["detail"] and "CHINNU" in crit[0]["detail"]
    booked = [f for f in fl if f["code"] == "HOLIDAY_RECEIPTS_BOOKED"]
    assert booked and booked[0]["severity"] == "medium" and "BINDU" in booked[0]["detail"]
    assert "HOLIDAY_RECEIPTS_PENDING" not in [f["code"] for f in fl]


def test_public_holiday_and_backdated_sunday_entry(conn, tmp_path):
    tue, wed, thu = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)
    r1, bc, kc = _day(conn, tmp_path, 1, tue, 100000, [_upi("626900001234", "ANU", 500)],
                      [dict(vno=1, name="ANU", loan_ac="01", instr="1234", receipt=500)])
    # Wednesday: a bank holiday — nothing in the bank book, one UPI and one sweep-like debit arrive
    r2, bc, kc = _day(conn, tmp_path, 2, wed, bc,
                     [_upi("626900005555", "ELSY", 1200), ("TR FROM CHERAI BRANCH 58.73.1137", 300, 0)], [], book_op=kc)
    c = codes(conn, r2)
    assert "HOLIDAY_RECEIPTS_PENDING" in c and "HOLIDAY_DEBIT_PENDING" in c
    assert "BANK_DEBIT_NOT_IN_BOOK" not in c
    # Thursday: neither is booked -> both critical, citing the holiday date
    r3, _, _ = _day(conn, tmp_path, 3, thu, bc, [_upi("626900006666", "FEMY", 100)],
                    [dict(vno=2, name="FEMY", loan_ac="06", instr="6666", receipt=100)], book_op=kc)
    fl = {f["code"]: f for f in flags(conn, r3)}
    assert fl["HOLIDAY_RECEIPT_NOT_BOOKED"]["severity"] == "critical"
    assert "30-09-2026" in fl["HOLIDAY_RECEIPT_NOT_BOOKED"]["detail"]
    assert fl["HOLIDAY_DEBIT_NOT_BOOKED"]["severity"] == "critical"


def test_book_entry_dated_sunday_is_flagged(conn, tmp_path):
    sun = date(2026, 9, 27)
    rid, _, _ = _day(conn, tmp_path, 1, sun, 100000, [_upi("626900001111", "ASHA MOL", 800)],
                     [dict(vno=9, name="ASHA MOL", loan_ac="02", instr="1111", receipt=800)])
    f = [x for x in flags(conn, rid) if x["code"] == "BOOK_ENTRY_ON_SUNDAY"]
    assert f and f[0]["severity"] == "high" and "Vchr 9" in f[0]["detail"]


def test_holiday_run_pages_render(conn, tmp_path):
    import re
    from io import BytesIO
    import openpyxl
    from fastapi.testclient import TestClient
    from app.main import app
    sat, sun = date(2026, 9, 26), date(2026, 9, 27)
    _, bc, kc = _day(conn, tmp_path, 1, sat, 100000, [_upi("626900001234", "ANU", 500)],
                     [dict(vno=1, name="ANU", loan_ac="01", instr="1234", receipt=500)])
    r2, _, _ = _day(conn, tmp_path, 2, sun, bc, [_upi("626900001111", "ASHA MOL", 800)], [], book_op=kc)
    c = TestClient(app, follow_redirects=False)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/setup").text).group(1)
    c.post("/setup", data={"username": "kev", "password": "abcdefghijk1", "password2": "abcdefghijk1", "csrf_token": tok})
    page = c.get(f"/runs/{r2}")
    assert page.status_code == 200 and "is a holiday" in page.text and "received on a holiday" in page.text
    x = c.get(f"/runs/{r2}/excel")
    assert x.status_code == 200
    wb = openpyxl.load_workbook(BytesIO(x.content))
    assert "holiday" in wb["BRS"]["A13"].value
    assert any("Holiday: received Sun 27-09-2026" == (r[10].value or "") for r in wb["Open Items"].iter_rows(min_row=4))
