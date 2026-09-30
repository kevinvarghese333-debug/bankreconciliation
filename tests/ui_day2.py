import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8077"; OUT=Path("/tmp/brweb/shots"); D=Path("/tmp/brweb/day27")
errs=[]
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1400,"height":900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE+"/login"); pg.fill("input[name=username]","kev"); pg.fill("input[name=password]","correct-horse-1"); pg.click("button.primary"); pg.wait_for_load_state()
    pg.goto(BASE+"/reconcile?account=1")
    print("hint:", pg.locator("#nexthint").inner_text())
    pg.set_input_files("input[name=bank_file]", str(D/"SIB_1113_27-09-2026.xlsx"))
    pg.set_input_files("input[name=book_file]", str(D/"BankBook_1113_27-09-2026.xlsx"))
    pg.click("#go"); pg.wait_for_load_state(); print(pg.url)
    pg.screenshot(path=str(OUT/"9_day27.png"), full_page=False)
    pg.locator("#detail").screenshot(path=str(OUT/"10_open.png"))
    # tick the bank charge and bindu -> diff shown, button disabled
    picks=pg.locator(".pick"); print("open picks:", picks.count())
    picks.nth(0).check(); picks.nth(1).check()
    print("selbar:", pg.locator("#selbar").inner_text().replace("\n"," | "), "disabled:", pg.locator("#sel-go").is_disabled())
    pg.screenshot(path=str(OUT/"11_select.png"))
    # add a note
    note=pg.locator(".notef input[name=note]").first; note.fill("Asked branch to pass JV for charges"); note.press("Tab"); pg.wait_for_load_state()
    pg.goto(BASE+"/open-items?account=1"); pg.screenshot(path=str(OUT/"12_openitems.png"), full_page=True)
    b.close()
print("errors", errs)
