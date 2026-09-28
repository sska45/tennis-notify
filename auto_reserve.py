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
# 開始時刻(HHMM) → 空き状況グリッドの時間帯番号(tzoneNo)。セルidは "YYYYMMDD_tzoneNo"
START_TO_TZONE = {900: 10, 1100: 20, 1300: 30, 1500: 40, 1700: 50, 1900: 60}

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

    # 空き状況ページ（施設ごと）へ遷移。
    # 注意: ここで page.goto(BASE) すると公開トップを読み直してログイン状態が切れるため、
    # 認証済みセッションのまま doAction で「施設の予約(#free-search)」へ遷移する。
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

    # グリッドは自動描画されないため、確定後に週表示を強制描画する
    use_day = target["date"].replace("-", "")     # YYYYMMDD
    tzone = START_TO_TZONE[target["startTime"]]
    cell_id = f"{use_day}_{tzone}"                 # 例: 20261019_30

    page.wait_for_function("() => typeof getWeekInfoAjax === 'function'", timeout=15000)
    page.evaluate("() => getWeekInfoAjax(11, 0, 0)")  # 今日起点の週を描画
    page.wait_for_timeout(2500)

    # 週表示はdaystart起点の7日窓。対象セルidが現れるまで「次週」で送る。
    # 次週送りは非同期のため、viewDay1が実際に変わるのを待ってから次へ進める。
    found = False
    steps = 0
    for i in range(12):
        if page.evaluate("(id) => !!document.getElementById(id)", cell_id):
            found = True
            break
        prev = page.evaluate("() => document.form1.viewDay1 ? document.form1.viewDay1.value : ''")
        page.evaluate("() => getWeekInfoAjax(4, 0, 0)")  # 次週>>
        try:
            page.wait_for_function(
                "(prev) => document.form1.viewDay1 && document.form1.viewDay1.value !== prev",
                arg=prev, timeout=10000)
        except Exception:
            page.wait_for_timeout(1500)
        page.wait_for_timeout(400)
        steps += 1
    log(f"  対象週へ移動: {'到達' if found else '未到達'}（cell_id={cell_id}, 次週送り{steps}回）")
    shot(page, f"01_vacant_{tag}")

    if not found:
        log("  対象セルが表示されませんでした。スクショを共有してください。")
        return False

    # 対象セルの状態を確認（available か、onclick が setReserv か）
    diag = page.evaluate(
        """(id) => {
            const td = document.getElementById(id);
            if (!td) return {exists:false};
            return {exists:true, cls:td.className, onclick:(td.getAttribute('onclick')||'').slice(0,80),
                    selected:td.getAttribute('data-selected')};
        }""",
        cell_id,
    )
    log("  対象セル:", json.dumps(diag, ensure_ascii=False))

    if "available" not in (diag.get("cls") or ""):
        log("  対象セルが空き状態ではありません（埋まった可能性）。中止。")
        return False

    # セルをクリック（ログイン時は onclick=setReserv が発火して枠選択される）
    page.evaluate("(id) => document.getElementById(id).click()", cell_id)
    page.wait_for_timeout(1800)
    shot(page, f"02_selected_{tag}")

    # 選択後に現れる操作ボタンを診断（ナビの「予約」メニューと区別するため）
    btns = page.evaluate(
        """() => [...document.querySelectorAll('button, a.btn, input[type=button], input[type=submit]')]
              .map(e => ({t: (e.innerText||e.value||'').trim().replace(/\\s+/g,' ').slice(0,20),
                          oc: (e.getAttribute('onclick')||'').slice(0,70)}))
              .filter(x => x.t || x.oc)"""
    )
    log("  選択後ボタン:", json.dumps(btns, ensure_ascii=False)[:600])

    # 予約申込へ（checkSelect → ReservedApplyAction）。ナビ以外の申込ボタンを優先
    proceeded = page.evaluate(
        """() => {
            const cands = [...document.querySelectorAll('button, a.btn, input[type=button], input[type=submit]')];
            // ReservedApply/checkSelect を呼ぶボタンを最優先
            let b = cands.find(e => /ReservedApply|checkSelect/.test(e.getAttribute('onclick')||''));
            // 次点：テキストが「予約」で始まる操作ボタン（ナビの「予約」メニューは <a> のヘッダ内なので除外気味）
            if (!b) b = cands.find(e => /^(予約|申込|確保|次へ|確認)/.test((e.innerText||e.value||'').trim())
                                        && !e.closest('nav') && !e.closest('header'));
            if (b) { b.click(); return (b.innerText||b.value||'').trim() + ' | ' + (b.getAttribute('onclick')||''); }
            return null;
        }"""
    )
    log("  予約申込ボタン:", proceeded)
    page.wait_for_timeout(800)
    # penaltyやconfirmのダイアログが出たら内容を捕捉（DRY_RUNでは先に進めない）
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
