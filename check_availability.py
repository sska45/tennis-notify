import os
import json
import time
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formatdate
from datetime import datetime, timedelta, timezone

# ── 設定 ───────────────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
BASE = "https://kouen.sports.metro.tokyo.lg.jp/web/"

# 監視する公園（テニス／人工芝コート）
# bcd=公園コード, icd=施設コード（解析で特定済み）
PARKS = [
    {"name": "猿江恩賜公園", "bcd": "1040", "icd": "10400030"},
    {"name": "木場公園",     "bcd": "1060", "icd": "10600010"},
]

WEEKS_AHEAD = int(os.environ.get("WEEKS_AHEAD", "5"))  # 何週間先まで確認するか
STATE_FILE = "state.json"

# 通知条件：平日は EVENING_FROM 以降のみ、土日は全枠
EVENING_FROM = 1900  # HHMM。平日はこの開始時刻以降の枠だけ通知

# メール設定（環境変数）
GMAIL_USER = os.environ.get("GMAIL_USER", "")             # 送信元Gmailアドレス
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # アプリパスワード
NOTIFY_TO = os.environ.get("NOTIFY_TO", GMAIL_USER)       # 通知先（未指定なら送信元と同じ）

# 空き状況Actionの完全POST本文テンプレート（実機解析で取得。bcdのみ差し替え）
INST_SRCH_BODY = (
    "daystarthome={today}&daystart={today}&selectPpsClPpscd=1000_1030"
    "&penaltyday=%5Bundefined%5D&dayofweekClearFlg=1&timezoneClearFlg=1"
    "&selectAreaBcd={bcd}&selectIcd=0&item540=%8Ew%92%E8%82%C8%82%B5"
    "&selectPpsClsCd=1000&selectPpsCd=1030&selectBldCd={bcd}"
    "&displayNo=pawab2000&displayNoFrm=pawab2000"
)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ── 状態管理（×→○の差分検知用） ─────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


# ── サイトアクセス ───────────────────────────────────────────────────────────

def new_session():
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def fetch_park(park):
    """1公園分の空き枠（status==0）を取得して返す。
    戻り値: [{date:'YYYY-MM-DD', start:1900, end:2100}, ...]
    """
    import requests
    today = datetime.now(JST).strftime("%Y-%m-%d")
    s = new_session()

    # 1) トップでセッション確立
    s.get(BASE, timeout=20)
    # 2) 空き状況Action（サーバー側に公園・目的の選択状態をセット）
    s.post(BASE + "rsvWOpeInstSrchVacantAction.do",
           data=INST_SRCH_BODY.format(today=today, bcd=park["bcd"]), timeout=20)
    # 3) 施設一覧Ajax（同上）
    s.post(BASE + "rsvWOpeInstSrchVacantBuildAjaxAction.do",
           data=f"displayNo=prwre1000&bldCd={park['bcd']}", timeout=20)

    # 4) 週単位で空き状況JSONを取得（WEEKS_AHEAD週分）
    available = []
    base_day = datetime.now(JST).date()
    for w in range(WEEKS_AHEAD):
        use_day = (base_day + timedelta(days=7 * w)).strftime("%Y%m%d")
        r = s.post(BASE + "rsvWOpeInstSrchVacantAjaxAction.do", data={
            "displayNo": "prwrc2000", "useDay": use_day,
            "bldCd": park["bcd"], "instCd": park["icd"],
            "transVacantMode": "11", "clearFlag": "0",
        }, timeout=20)
        r.encoding = "cp932"
        if "ErrManager" in r.text or '"result"' not in r.text:
            time.sleep(1)
            continue
        data = json.loads(r.text)
        for tzone in data.get("result", []):
            for slot in tzone.get("timeResult", []):
                if slot.get("status") == 0:  # 0 = 空き
                    d = datetime.strptime(str(slot["useDay"]), "%Y%m%d").date()
                    available.append({
                        "date": d.strftime("%Y-%m-%d"),
                        "weekday": d.weekday(),
                        "start": slot["startTime"],
                        "end": slot["endTime"],
                    })
        time.sleep(0.5)
    return available


# ── フィルタ：平日19時以降＋土日全枠 ──────────────────────────────────────────

def passes_filter(slot):
    if slot["weekday"] >= 5:   # 5=土, 6=日 → 全枠
        return True
    return slot["start"] >= EVENING_FROM  # 平日は19時以降のみ


def fmt_time(hhmm):
    return f"{hhmm // 100:02d}:{hhmm % 100:02d}"


WD_JP = ["月", "火", "水", "木", "金", "土", "日"]


def slot_label(park_name, slot):
    return (f"{park_name}　{slot['date']}（{WD_JP[slot['weekday']]}）"
            f"{fmt_time(slot['start'])}〜{fmt_time(slot['end'])}")


# ── メール通知 ───────────────────────────────────────────────────────────────

def send_mail(subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and NOTIFY_TO):
        print("【メール未送信】GMAIL_USER / GMAIL_APP_PASSWORD / NOTIFY_TO が未設定です。")
        print(f"--- 件名: {subject}\n{body}")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_TO
    msg["Date"] = formatdate(localtime=True)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [a.strip() for a in NOTIFY_TO.split(",")], msg.as_string())
    print(f"メール送信完了 → {NOTIFY_TO}")


# ── メイン ───────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    prev = set(state.get("available_keys", []))  # 前回の空き枠キー
    current = set()        # 今回の空き枠キー
    slot_by_key = {}       # キー → 表示用ラベル
    errors = []

    for park in PARKS:
        try:
            slots = fetch_park(park)
            hit = [s for s in slots if passes_filter(s)]
            print(f"{park['name']}: 取得{len(slots)}枠 / 条件一致{len(hit)}枠")
            for s in hit:
                key = f"{park['bcd']}|{s['date']}|{s['start']}"
                current.add(key)
                slot_by_key[key] = slot_label(park["name"], s)
        except Exception as e:
            errors.append(f"{park['name']}: {e}")
            print(f"{park['name']}: エラー — {e}")

    # ×→○ に変わった（前回なくて今回ある）枠だけ通知
    newly = sorted(current - prev, key=lambda k: slot_by_key[k])

    if newly:
        lines = ["🎾 テニスコートに空きが出ました！\n"]
        lines += [f"・{slot_by_key[k]}" for k in newly]
        lines.append("\n▼ 予約はこちら（ログイン後：施設の予約 → テニス（人工芝）→ 公園名）")
        lines.append(BASE)
        body = "\n".join(lines)
        subject = f"🎾 テニス空き {len(newly)}件（猿江/木場）"
        send_mail(subject, body)
        print(f"新規の空き {len(newly)}件を通知しました")
    else:
        print("新規の空きなし（通知なし）")

    # エラーがあっても、取得できた公園の状態は保存する。
    # 取得できなかった公園の前回キーは保持し、誤って「消えた」扱いにしない。
    failed_bcds = {e.split(":")[0] for e in errors}
    if failed_bcds:
        # 失敗した公園のキーは前回値を引き継ぐ
        for k in prev:
            bcd = k.split("|")[0]
            park_name = next((p["name"] for p in PARKS if p["bcd"] == bcd), None)
            if park_name in failed_bcds:
                current.add(k)

    state["available_keys"] = sorted(current)
    state["last_run"] = datetime.now(JST).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
