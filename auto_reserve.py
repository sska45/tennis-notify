"""
猿江/木場テニスコートの自動予約（DRY_RUN対応）

- 対象日の「午後(13:00-21:00)」枠が空いたら自動予約する
- 安全のため、利用日の SAFE_BUFFER_DAYS 日以上先の枠のみ対象
  （テニスは利用日の4日前までキャンセル無料。それより直前の予約は避ける）
- DRY_RUN=true（既定）の間は「予約確定」は絶対に行わず、確認画面手前で
  スクリーンショットとHTMLを ./artifacts/ に保存して停止する
- ログインID/PWは環境変数 TOMIN_USER_ID / TOMIN_PASSWORD から読む（コードに書かない）

必要: pip install playwright requests && playwright install chromium
"""
import os
import re
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
BASE = "https://kouen.sports.metro.tokyo.lg.jp/web/"

# ── 予約対象 ────────────────────────────────────────────────────────────────
TARGET_PARKS = [
    {"name": "猿江恩賜公園", "bcd": "1040", "icd": "10400030", "pps": "1030"},
    {"name": "木場公園",     "bcd": "1060", "icd": "10600010", "pps": "1030"},
]
TARGET_DATES = [
    "2026-10-17", "2026-10-18", "2026-10-31",
    "2026-11-07", "2026-11-14", "2026-11-15", "2026-11-28", "2026-11-29",
]
# 午後＝13:00〜21:00開始の枠（13-15/15-17/17-19/19-21）
AFTERNOON_STARTS = {1300, 1500, 1700, 1900}

SAFE_BUFFER_DAYS = 6          # 利用日の6日以上先の枠のみ自動予約
RESERVED_FILE = "reserved.json"

DRY_RUN = os.environ.get("AUTO_RESERVE_DRYRUN", "true").lower() != "false"
USER_ID = os.environ.get("TOMIN_USER_ID", "")
PASSWORD = os.environ.get("TOMIN_PASSWORD", "")

# ── テスト用の一時上書き（本番設定は変えずにフロー捕捉するため） ──────────────
# 例: TEST_DATE=2026-10-19 TEST_BCD=1040 python3 auto_reserve.py
_TEST_DATE = os.environ.get("TEST_DATE", "")
_TEST_BCD = os.environ.get("TEST_BCD", "")
if _TEST_DATE:
    TARGET_DATES = [_TEST_DATE]
    SAFE_BUFFER_DAYS = 0  # テスト時はバッファ無効（捕捉目的）
if _TEST_BCD:
    TARGET_PARKS = [p for p in TARGET_PARKS if p["bcd"] == _TEST_BCD]

ART = "artifacts"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

INST_SRCH_BODY = (
    "daystarthome={today}&daystart={today}&selectPpsClPpscd=1000_{pps}"
    "&penaltyday=%5Bundefined%5D&dayofweekClearFlg=1&timezoneClearFlg=1"
    "&selectAreaBcd={bcd}&selectIcd=0&item540=%8Ew%92%E8%82%C8%82%B5"
    "&selectPpsClsCd=1000&selectPpsCd={pps}&selectBldCd={bcd}"
    "&displayNo=pawab2000&displayNoFrm=pawab2000"
)


def log(*a):
    print(f"[{datetime.now(JST):%H:%M:%S}]", *a, flush=True)


def load_reserved():
    try:
        with open(RESERVED_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_reserved(d):
    with open(RESERVED_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


# ── 空き確認（requests・軽量）: 対象の空き枠を探す ─────────────────────────────

def find_open_targets(reserved):
    """対象日×対象公園の午後枠のうち、いま空いていて、かつ安全バッファを満たすものを返す。"""
    today = datetime.now(JST).date()
    earliest = today + timedelta(days=SAFE_BUFFER_DAYS)  # これ以降のみ
    wanted_dates = {d for d in TARGET_DATES
                    if datetime.strptime(d, "%Y-%m-%d").date() >= earliest}
    if not wanted_dates:
        return []

    open_targets = []
    for park in TARGET_PARKS:
        s = requests.Session()
        s.headers.update({"User-Agent": UA,
                          "Content-Type": "application/x-www-form-urlencoded",
                          "X-Requested-With": "XMLHttpRequest"})
        s.get(BASE, timeout=20)
        s.post(BASE + "rsvWOpeInstSrchVacantAction.do",
               data=INST_SRCH_BODY.format(today=today.strftime("%Y-%m-%d"),
                                          bcd=park["bcd"], pps=park["pps"]), timeout=20)
        s.post(BASE + "rsvWOpeInstSrchVacantBuildAjaxAction.do",
               data=f"displayNo=prwre1000&bldCd={park['bcd']}", timeout=20)
        # 対象日をカバーする週をまとめて取得
        weeks = sorted({datetime.strptime(d, "%Y-%m-%d").date() for d in wanted_dates})
        seen = set()
        for wd in weeks:
            use_day = wd.strftime("%Y%m%d")
            if use_day in seen:
                continue
            seen.add(use_day)
            r = s.post(BASE + "rsvWOpeInstSrchVacantAjaxAction.do", data={
                "displayNo": "prwrc2000", "useDay": use_day,
                "bldCd": park["bcd"], "instCd": park["icd"],
                "transVacantMode": "11", "clearFlag": "0",
            }, timeout=20)
            r.encoding = "cp932"
            if "ErrManager" in r.text or '"result"' not in r.text:
                continue
            for tzone in json.loads(r.text).get("result", []):
                for slot in tzone.get("timeResult", []):
                    if slot.get("status") != 0:
                        continue
                    d = datetime.strptime(str(slot["useDay"]), "%Y%m%d").date().strftime("%Y-%m-%d")
                    if d not in wanted_dates or slot["startTime"] not in AFTERNOON_STARTS:
                        continue
                    key = f"{park['bcd']}|{d}|{slot['startTime']}"
                    if reserved.get(key):  # 予約済みはスキップ
                        continue
                    open_targets.append({
                        "park": park, "key": key, "date": d,
                        "startTime": slot["startTime"], "endTime": slot["endTime"],
                        "tzoneNo": tzone["tzoneNo"],
                    })
            time.sleep(0.4)
    return open_targets


# ── 予約（Playwright・ログインが必要） ─────────────────────────────────────────

def shot(page, name):
    os.makedirs(ART, exist_ok=True)
    path = os.path.join(ART, name)
    try:
        page.screenshot(path=path + ".png", full_page=True)
        with open(path + ".html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log("  captured:", path + ".png / .html")
    except Exception as e:
        log("  capture失敗:", e)


def reserve_one(page, target):
    """1件の枠を予約する。DRY_RUN中は確定せず確認画面手前で停止し捕捉する。"""
    park = target["park"]
    tag = f"{park['bcd']}_{target['date']}_{target['startTime']}"
    log(f"予約試行: {park['name']} {target['date']} {target['startTime']}")

    # 空き状況ページ（施設ごと）へ遷移
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(600)
    page.evaluate("doAction(document.form1, gRsvWOpeHomeAction + '#free-search')")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)
    page.select_option("#purpose-home", label="テニス（人工芝）")
    page.wait_for_timeout(1500)
    page.select_option("#bname-home", label=park["name"])
    page.wait_for_timeout(500)
    page.evaluate("doSearchHome(document.form1, gRsvWOpeInstSrchVacantAction)")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    shot(page, f"01_vacant_{tag}")

    # 対象日・時間帯の「空き(●)」セルを探してクリック
    # グリッドは動的描画のため、useDay(YYYYMMDD)とstartTimeを手掛かりに探索する
    use_day = target["date"].replace("-", "")
    clicked = page.evaluate(
        """({useDay, startTime}) => {
            // setReserv(idName,bCd,iCd,iDay,startTime,endTime,tzoneNo) を呼ぶ要素を探す
            const els = [...document.querySelectorAll('[onclick*="setReserv"]')];
            for (const el of els) {
                const oc = el.getAttribute('onclick');
                if (oc.includes(String(useDay)) && oc.includes(String(startTime))) {
                    el.click();
                    return oc;
                }
            }
            return null;
        }""",
        {"useDay": use_day, "startTime": target["startTime"]},
    )
    log("  セル検索結果:", clicked)
    page.wait_for_timeout(1500)
    shot(page, f"02_selected_{tag}")

    if not clicked:
        log("  対象セルが見つかりませんでした（描画待ち/構造要確認）。捕捉のみで終了。")
        return False

    # 「予約」ボタンへ（checkSelect → ReservedApplyAction）
    btn = page.query_selector("button:has-text('予約'), a:has-text('予約')")
    if btn:
        btn.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
    shot(page, f"03_after_reserve_click_{tag}")

    if DRY_RUN:
        log("  DRY_RUN: ここで停止（確定しません）。確認画面を捕捉しました。")
        return False

    # ── ここから先（本番の確定）は、DRY_RUNの捕捉結果を見てから実装する ──
    log("  本番確定ロジックは未実装（DRY_RUNの捕捉後に追加）")
    return False


def login(page):
    log("ログイン中...")
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(500)
    page.evaluate("doAction(document.form1, gRsvWTransUserLoginAction)")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)
    page.fill('input[name="userId"]', USER_ID)
    page.fill('input[name="password"]', PASSWORD)
    shot(page, "00_login_filled")
    # ログインボタン
    page.evaluate("""
        () => {
            const b = [...document.querySelectorAll('button,a')].find(e => /ログイン/.test(e.innerText));
            if (b) b.click();
        }
    """)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    shot(page, "00b_after_login")
    body = page.inner_text("body")
    if "利用者番号" in body or "ログアウト" in body:
        log("  ログイン成功とみられます")
        return True
    log("  ログイン結果が不明（00b_after_login を確認してください）")
    return True  # 続行（捕捉のため）


def main():
    if not (USER_ID and PASSWORD):
        log("TOMIN_USER_ID / TOMIN_PASSWORD が未設定です。環境変数に設定してください。")
        sys.exit(1)

    log(f"=== 自動予約 {'[DRY_RUN]' if DRY_RUN else '[本番]'} ===")
    reserved = load_reserved()
    targets = find_open_targets(reserved)
    log(f"予約候補（空き×安全バッファ内）: {len(targets)}件")
    for t in targets:
        log(f"  - {t['park']['name']} {t['date']} {t['startTime']}-{t['endTime']}")
    if not targets:
        log("対象なし。終了。")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(locale="ja-JP", user_agent=UA)
        page = ctx.new_page()
        login(page)
        # まずは1件だけ試す（捕捉目的）
        for t in targets[:1]:
            ok = reserve_one(page, t)
            if ok and not DRY_RUN:
                reserved[t["key"]] = {
                    "reservedAt": datetime.now(JST).isoformat(),
                    "park": t["park"]["name"], "date": t["date"],
                    "start": t["startTime"], "end": t["endTime"],
                }
                save_reserved(reserved)
        browser.close()
    log("完了。./artifacts/ のスクショ・HTMLを確認してください。")


if __name__ == "__main__":
    main()
