"""Click through the whole app in a real browser and save screenshots."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/brweb/shots"); OUT.mkdir(parents=True, exist_ok=True)
S = Path(__file__).resolve().parent.parent / "samples"
errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(BASE + "/")
    pg.fill("input[name=username]", "kev"); pg.fill("input[name=password]", "correct-horse-1"); pg.fill("input[name=password2]", "correct-horse-1")
    pg.click("button"); pg.wait_for_load_state()
    pg.fill("input[name=acc_no]", "1113"); pg.fill("input[name=nickname]", "SIB 1113 · Main disbursement")
    pg.select_option("select[name=kind]", "main"); pg.fill("input[name=branch]", "North Paravur")
    pg.click("#form button.primary"); pg.wait_for_load_state()
    pg.fill("input[name=acc_no]", "2201"); pg.fill("input[name=nickname]", "SIB 2201 · Kunnukara collection")
    pg.select_option("select[name=kind]", "collection"); pg.click("#form button.primary"); pg.wait_for_load_state()
    pg.screenshot(path=str(OUT / "1_accounts.png"), full_page=True)
    pg.goto(BASE + "/reconcile")
    pg.select_option("#acc", label=pg.locator("#acc option").nth(1).inner_text())
    pg.set_input_files("input[name=bank_file]", str(S / "SIB_1113_26-09-2026.xls"))
    pg.set_input_files("input[name=book_file]", str(S / "BankBook_1113_26-09-2026.xlsx"))
    pg.screenshot(path=str(OUT / "2_upload.png"), full_page=True)
    pg.click("#go"); pg.wait_for_load_state()
    pg.screenshot(path=str(OUT / "2b_first_run_confirm.png"), full_page=True)
    print("confirm page:", pg.locator("h1").inner_text())
    pg.fill("input[name=reason]", "Confirmed: statement is SIB 1113, opening 1,90,56,309.27")
    pg.click("text=Acknowledge and reconcile"); pg.wait_for_load_state()
    print("after upload:", pg.url, pg.title())
    pg.screenshot(path=str(OUT / "3_run.png"), full_page=False)
    pg.click('.tab[data-tab="t-out"]'); pg.check('[data-reviewonly="#tb-out"]')
    pg.locator("#t-out").screenshot(path=str(OUT / "4_payments_review.png"))
    pg.click('.tab[data-tab="t-in"]')
    pg.locator("#t-in").screenshot(path=str(OUT / "5_collections.png"))
    with pg.expect_download() as d:
        pg.click("text=Download Excel")
    d.value.save_as(str(OUT / d.value.suggested_filename)); print("excel:", d.value.suggested_filename)
    # duplicate upload blocked
    pg.goto(BASE + "/reconcile")
    pg.select_option("#acc", index=1)
    pg.set_input_files("input[name=bank_file]", str(S / "SIB_1113_26-09-2026.xls"))
    pg.set_input_files("input[name=book_file]", str(S / "BankBook_1113_26-09-2026.xlsx"))
    pg.click("#go"); pg.wait_for_load_state()
    pg.screenshot(path=str(OUT / "6_blocked.png"), full_page=True)
    print("blocked page h1:", pg.locator("h1").inner_text())
    pg.goto(BASE + "/"); pg.screenshot(path=str(OUT / "7_dashboard.png"), full_page=True)
    for u in ["/history", "/open-items", "/audit"]:
        pg.goto(BASE + u); assert pg.locator("h1").count(), u
    pg.goto(BASE + "/history"); pg.screenshot(path=str(OUT / "8_history.png"), full_page=True)
    b.close()
print("console errors:", errors)
