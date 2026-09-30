# Bank Reco — daily bank reconciliation for Freemen Investments

Upload the **bank statement** (South Indian Bank net-banking `.xls`) and the **Bank Book** export from the ERP
for the same account and date. The app validates both files, matches every line, carries unmatched items
forward to the next day, and produces a **Bank Reconciliation Statement** (on screen and in Excel) that must
show a nil difference.

Runs fully offline. No AI service and no API costs. All data stays in one SQLite file in `data/`.

---

## 1. Run it on your computer (localhost)

**Option A: Python (simplest on a Mac)**

```bash
cd bankreco
chmod +x start.sh
./start.sh
```

The first start sets itself up and takes about a minute. The app then opens at **http://localhost:8077**.
On the first visit you create your login.

**Option B: Docker Desktop**

```bash
cd bankreco
cp .env.example .env
docker compose up -d --build
```

Open http://localhost:8077.

## 2. First-time setup in the app

1. Create your login (one user, password of 10+ characters).
2. **Accounts → Add account** for each bank account:
   - `1113`, type **Main**: disbursements and payments.
   - The 33 collection accounts, type **Collection**. For these, only receipts and the day-end sweep are expected.
3. **New reconciliation**: pick the account, then drop in the two files.

The first run of an account asks you to acknowledge its starting position. From then on, each day must follow
on from the previous day.

## 3. Daily routine

On the 29th, reconcile the 28th. For each account:

1. From net banking, download the account statement for the day (Excel).
2. From the ERP, download the Bank Book for that account's ledger, same day, all branches.
3. **New reconciliation**: choose the account, upload both files, press **Reconcile**.
4. Check the BRS shows **difference ₹0.00**, then work through the **Exceptions** list:
   - **Critical**: money left the bank with no book entry, or an item is open 3+ days. Act the same day.
   - **High**: an entry is in the book but not in the bank, or a possible double payment.
   - **Medium**: a payment matched on amount but the names differ, or collections that agree only on the day total. Verify these.
   - **Info**: data-entry quality issues, such as a placeholder UPI ref like "2525".
5. **Download Excel** for the file, and get it signed in the prepared/checked/approved boxes.

Unmatched items stay in **Open items** and clear automatically when the other side arrives on a later day.
If needed, tick a bank line and the book entry it belongs to, then **Match selected**. A remark is required,
the amounts must be exactly equal, and every manual match is logged.

## 4. The controls it enforces

| Check | What stops the run |
|---|---|
| Statement integrity | The running balance is recomputed on every row. A deleted or edited row breaks the chain and **cannot be overridden**. |
| Bank book integrity | For every branch: Opening + receipts − payments = Closing, and the "Total" row equals the entries. |
| Same period | Every bank transaction must fall inside the Bank Book period. |
| No double use | The same file can never be reconciled twice (checked by file fingerprint). |
| Sequence | Dates run in order. To redo a date, undo later runs first (latest-first). |
| Continuity | Today's opening must equal yesterday's closing, for both bank and book. Back-dated ERP entries are caught here. |
| Opening identity | Bank opening − book opening must equal the open items carried forward. |

A gap in dates or a continuity break can be overridden only with a written reason. The reason is printed
on the BRS and stored in the audit log.

### Sundays and bank holidays

Branches are closed on Sundays and public bank holidays, but UPI money still reaches the bank. Reconcile those
days like any other: upload the bank statement and that day's ERP bank book (downloading it on Monday is fine).

- **What counts as a holiday:** every Sunday, any day on the **Holidays** page (paste the year's bank holidays,
  one per line: `02-10-2026 Gandhi Jayanthi`), and any day whose bank book has no entries.
- **On the holiday:** money received is shown as *"waiting for the next working day's bank book"* (Info), and as a
  memo line under C in the BRS. Money that left the account is flagged High.
- **Next working day:** holiday items are matched against that day's bank book, including pooling with that
  day's receipts. Anything still missing becomes **Critical**, stating the date the fund reached the account.
- **Entries on closed days:** book entries dated on a Sunday or a listed holiday are flagged High, because they
  were probably back-dated.
- **Instrument date:** for holiday receipts booked later, the app reports whether the ERP instrument date
  (`Dt.:` in Instrument.Det) matches the date the bank received the money.
- **Ageing:** "open 3+ days" counts working days only.

## Users, roles and the Admin portal

The first login created at setup is an **Admin**. The Admin portal is at **/bankreco/admin**.

| Role | Can do |
|---|---|
| Viewer | Read everything and download Excel |
| Operator | + upload and reconcile, manual match, notes |
| Manager | + acknowledge warnings, unmatch, undo the latest run, sign off runs as checked, holidays, audit log |
| Admin | + users and roles, revoke reconciled days, bank accounts |

- **Users:** create a user with a temporary password; they must set their own at first sign-in. Changing a role,
  disabling a user or resetting their password signs them out immediately. The last active Admin cannot be
  demoted or disabled.
- **Revoke a day:** choose an account (or all accounts) and a date. The app previews every run from that date
  onward, which will be removed; a reason and typing REVOKE are required. A full copy of each revoked run is
  kept, and those days can then be reconciled again in order.
- **Sign-off (maker–checker):** a Manager or Admin marks a run "Checked". They cannot check their own run. The
  names print on the Excel BRS.

## 5. How matching works

Amounts must agree to the paisa. Debits only match payments, and credits only match receipts.

**Payments (bank debits vs book payments)**
1. Bank reference or cheque number found in the ERP entry, plus the amount.
2. Amount plus beneficiary name (85%+ similar). Bank narrations truncate names, so "LILLY" matches "LILLY JOHNSON".
3. Amount plus a partly similar name (60–85%). Marked **Verify**, because the payee may be the wrong person.
4. Amount only, when it is the only such amount in the 7-day window. Marked **Verify**.

**Collections (bank credits vs book receipts)**
1. UTR last-4 (the ERP "Instrument.Det" field) plus the amount.
2. UTR last-4 plus a group total: one UPI payment covering several loans.
3. Amount plus payer name.
4. One UPI covering several entries for the same person, or entries booked with the same (real) reference.
   Marked **Verify**.
5. Whatever is left for a day is **pooled** if both sides total exactly the same, and marked **Verify**.
   Otherwise it stays open.

Every **Verify** match appears in Exceptions and on the Excel "Payments Matched" and "Collections Matched" sheets.

## 6. Hosting on the Hostinger VPS — https://kavitha.cloud/bankreco/

One command from your Mac's Terminal (asks for the VPS root password twice):

```bash
scp ~/Downloads/bankreco.zip root@200.234.34.123:/root/ && ssh -t root@200.234.34.123 'apt-get install -y -qq unzip >/dev/null; cd /root && rm -rf bankreco && unzip -q bankreco.zip && bash bankreco/deploy/install.sh'
```

`deploy/install.sh` does the whole setup and is safe to re-run for upgrades:

1. Installs Docker if missing and runs the app in its own container, reachable only from inside the server
   (127.0.0.1:8077). Data lives in `/opt/bankreco/data`.
2. Checks that `kavitha.cloud` points to this server. If not, it tells you which DNS A record to add.
3. Finds the web server already on ports 80/443 and adds only the `/bankreco/` path to the kavitha.cloud site:
   - **Nginx:** inserts one location block into that site. The file is backed up first and restored
     automatically if `nginx -t` fails.
   - **Apache:** adds a proxy config for `/bankreco/` only.
   - **Nothing running:** installs Caddy.
   It then adds a free HTTPS certificate if the site doesn't have one, and makes the login cookie HTTPS-only.
4. Schedules a daily database backup at 23:30 (kept 60 days).

The rest of your website is not touched. The installer writes a log to `/var/log/bankreco-install.log`.

**Upgrading:** run the same command with the new zip. Your data is kept, and the database is backed up
before each upgrade.

**Useful commands on the VPS**

```bash
cd /opt/bankreco && docker compose logs -f     # live log
cd /opt/bankreco && docker compose restart     # restart
ls /opt/bankreco/data                          # database + daily backups
```

**Moving your local history to the VPS:** copy your local `data/bankreco.sqlite3` to
`/opt/bankreco/data/`, run `chown 10001:10001` on it, then `docker compose restart`.


### Automatic deployment from GitHub

Every push to `main` runs the tests and, if they pass, deploys to the VPS (`.github/workflows/deploy.yml`).

One-time setup:
1. On the VPS run `bash deploy/github-setup.sh` (from a copy of this repo). It creates a deploy key that can only
   receive the app and run the installer, and prints the private key.
2. In GitHub: repo → Settings → Secrets and variables → Actions → New repository secret →
   name `VPS_SSH_KEY`, value = the printed key.
3. Actions tab → "Test and deploy" → Run workflow (or push any change).

The manual zip/scp route above still works as a fallback.

## 7. Adding another bank's format

`app/parsers.py → parse_bank_statement` finds columns by header name. Headers such as "Narration",
"Debit", "Credit", "Withdrawal Amt", "Deposit Amt" and "Closing Balance" are already recognised, so many banks
work without changes. For a new narration style (for example HDFC NEFT), extend `parse_narration`.

## 8. Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The tests cover:
- the real 26-09-2026 SIB file, which must tally
- carry-forward across two days
- a tampered statement, which must be blocked
- a continuity break that needs a reason to proceed
- stray outflows from a collection account
- manual match and unmatch
- double-payment detection

## Files

```
app/parsers.py   reads SIB statements + ERP Bank Book, file-level validations
app/matcher.py   matching rules, exception flags, BRS arithmetic
app/engine.py    run workflow: continuity checks, carry-forward, manual match, undo
app/report.py    Excel output (BRS uses live formulas over the Open Items sheet)
app/main.py      web app (login, upload, results, open items, history, audit log)
samples/         the 26-09-2026 files used to build and test this
```
