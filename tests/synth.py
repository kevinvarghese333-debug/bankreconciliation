"""Generate synthetic SIB statements and ERP bank books in the real export layouts."""
from datetime import date

import openpyxl


def write_statement(path, d: date, opening: float, txns: list[tuple]):
    """txns: (narration, withdrawal, deposit)"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["SlNo", "Transaction Date", "Value Date", "Particulars", None, "Cheque Number",
               "Withdrawals", None, "Deposits", "Balance Amount"])
    bal = opening
    for n, t in enumerate(txns, 1):
        narr, wd, dp = t[:3]
        dd = t[3] if len(t) > 3 else d
        bal = round(bal - wd + dp, 2)
        ws.append([n, dd.strftime("%d-%m-%Y"), dd.strftime("%d-%m-%Y"), narr, None, None,
                   f"{wd:,.2f}" if wd else None, None, f"{dp:,.2f}" if dp else None, f"{bal:,.2f}"])
    ws.append([None, None, None, None, "****End of A/c Statement****"])
    wb.save(path)
    return bal


def write_book(path, d: date, opening: float, entries: list[dict], company="Freemen Investments Pvt. Ltd.",
               branch="North Paravur"):
    """entries: dict(vno, vtype, name, loan_ac, instr, receipt, payment, narration?)"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ds = d.strftime("%d-%m-%Y")
    ws.append([f"{company}\n\nBank Book from {ds} to {ds}"])
    ws.append(["Br.ID", "Branch", "Br. Code", "Tran.Date", "Vchr.Num.", "Vchr.Type", "Accounts",
               "Instrument.Det", "Narration", "Notes", "Debit", "Credit", "Balance", "Remarks"])
    ws.append([2, "Head Office", None, d, None, None, None, None, "Opening", None, 0, 0, 0, " - "])
    ws.append([2, "Head Office", None, d, None, None, None, None, "Total", None, 0, 0, None, " - "])
    ws.append([2, "Head Office", None, d, None, None, None, None, "Closing", None, 0, 0, None, " - "])
    ws.append([3, branch, None, d, None, None, None, None, "Opening", None, opening, 0, opening, " - "])
    bal = opening
    dr = cr = 0.0
    for e in entries:
        bal = round(bal + e.get("receipt", 0) - e.get("payment", 0), 2)
        dr += e.get("receipt", 0)
        cr += e.get("payment", 0)
        narr = e.get("narration") or (
            f"Direct Pay, A/c: PERS, A/c No: {e.get('loan_ac','')}, Name: {e['name']}" if e.get("payment")
            else f"Received against loan, A/c: JLGL, A/c No: {e.get('loan_ac','')}, Name: {e['name']}")
        ws.append([3, branch, None, d, e["vno"], e.get("vtype", "BP" if e.get("payment") else "BR"),
                   "x", (f"{e['instr']}, Dt.:{e.get('instr_date', d):%d-%b-%Y}" if e.get("instr") else None), narr, "",
                   e.get("receipt", 0), e.get("payment", 0), bal, ""])
    ws.append([3, branch, None, d, None, None, None, None, "Total", None,
               round(opening + dr, 2), round(cr, 2), None, " - "])
    ws.append([3, branch, None, d, None, None, None, None, "Closing", None, bal, 0, None, " - "])
    wb.save(path)
    return bal
