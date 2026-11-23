"""
臺北市運動中心即時人數爬蟲（面試作品集專案：運動中心人流 x 營運）
=====================================================================

每執行一次，就抓「一次」各館當下的游泳池、健身房人數與容留上限，
附加寫入 data/occupancy_YYYY-MM.csv（一個月一個檔案）。
每 15 分鐘執行一次的排程交給 GitHub Actions，這支程式只負責抓一次。

資料來源：智聯運動科技平台公開顯示的即時人數（回傳 JSON；網站未設 robots.txt 限制）
  北投、大安、大同、中正、南港、內湖、松山、文山，共 8 館

不列入：
  - 信義運動中心、永春活力館：經營單位的網站 robots.txt 表明不希望程式抓取 API，予以尊重
  - 中山運動中心：2026/10/1 起封館整修
  - 萬華運動中心：整修中（本專案重新開幕提案的對象）
  - 士林運動中心：網站會擋程式連線，且 2026/11/30 停止營運

用法：
  python crawler.py          正式執行：抓一次並寫入 CSV
  python crawler.py --test   試跑：只在畫面印出結果，並把原始回應存到 debug/，不寫入 CSV

需要的套件：
  pip install requests
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ──────────────────────────────────────────────
# 基本設定
# ──────────────────────────────────────────────
TZ = timezone(timedelta(hours=8), name="Asia/Taipei")  # 台灣沒有日光節約時間，固定 UTC+8
BASE_DIR = Path(__file__).resolve().parent             # 程式所在的資料夾
DATA_DIR = BASE_DIR / "data"                           # 正式資料
DEBUG_DIR = BASE_DIR / "debug"                         # 原始回應（試跑或抓取失敗時存放）
TIMEOUT = 20                                           # 最多等 20 秒

# 模擬一般瀏覽器；有些網站會擋掉沒有瀏覽器標頭的請求
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 智聯運動科技平台
SPO_PAGE = "https://booking-tpsc.sporetrofit.com/Home/LocationPeopleNum"
SPO_API = "https://booking-tpsc.sporetrofit.com/Home/loadLocationPeopleNum"
# 平台上也列出信義（XYSC）、中山（ZSSC），但信義的人數沒有串接到平台、中山封館中，
# 兩館在平台上一直是 0，抓了只會變成錯誤資料，所以用館別代碼略過
SPO_SKIP_LID = {"XYSC", "ZSSC"}

# CSV 欄位：時間、資料來源、館名、設施、現場人數、容留上限
CSV_FIELDS = ["timestamp", "source", "center", "facility", "people", "capacity"]


# ──────────────────────────────────────────────
# 共用小工具
# ──────────────────────────────────────────────
def to_int(value) -> int | None:
    """把 '60'、60、60.0 之類的值轉成整數；轉不出來就回傳 None（負數原樣保留，留到資料清理階段處理）"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def make_row(source: str, center: str, facility: str, people, capacity) -> dict:
    """統一每一筆資料的格式"""
    return {
        "source": source,
        "center": center,
        "facility": facility,
        "people": people,
        "capacity": capacity,
    }


def save_raw(filename: str, content: str) -> None:
    """把網站原始回應存到 debug/，方便檢查格式或找出錯誤原因"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / filename).write_text(content, encoding="utf-8")


def parse_or_keep_raw(filename: str, resp: requests.Response, parse, debug: bool) -> list[dict]:
    """解析回應；試跑模式一律存原始回應，正式模式只在解析失敗時存"""
    if debug:
        save_raw(filename, resp.text)
    try:
        return parse(resp.json())
    except Exception:
        save_raw(filename, resp.text)
        raise


# ──────────────────────────────────────────────
# 抓取與解析：智聯運動科技平台（8 館）
# ──────────────────────────────────────────────
def fetch_sporetrofit(session: requests.Session, debug: bool) -> list[dict]:
    # 步驟 1：先開一般網頁，取得網站發的 cookie
    session.get(SPO_PAGE, timeout=TIMEOUT).raise_for_status()

    # 步驟 2：模擬網頁自己發出的 AJAX 請求，取得各館人數（JSON）
    resp = session.post(
        SPO_API,
        timeout=TIMEOUT,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SPO_PAGE,
            "Origin": "https://booking-tpsc.sporetrofit.com",
        },
    )
    resp.raise_for_status()
    return parse_or_keep_raw("sporetrofit_api.json", resp, parse_sporetrofit, debug)


def parse_sporetrofit(data: dict) -> list[dict]:
    """回應格式（2026/11/23 實測）：
    {"locationPeopleNums": [
        {"LID": "BTSC", "lidName": "北投",
         "swPeopleNum": "74", "swMaxPeopleNum": "200",      ← 游泳池 人數 / 上限
         "gymPeopleNum": "44", "gymMaxPeopleNum": "60"},    ← 健身房 人數 / 上限
        ...]}"""
    rows = []
    for item in data["locationPeopleNums"]:
        if item["LID"] in SPO_SKIP_LID:
            continue
        center = f"{item['lidName']}運動中心"  # 網站顯示時也是在簡稱後面加「運動中心」
        rows.append(make_row("sporetrofit", center, "游泳池",
                             to_int(item["swPeopleNum"]), to_int(item["swMaxPeopleNum"])))
        rows.append(make_row("sporetrofit", center, "健身房",
                             to_int(item["gymPeopleNum"]), to_int(item["gymMaxPeopleNum"])))
    if not rows:
        raise ValueError("回應中沒有任何館的資料")
    return rows


# ──────────────────────────────────────────────
# 主程式
# ──────────────────────────────────────────────
def all_sources() -> list[tuple]:
    """要抓的資料來源（之後若取得其他場館同意，可以在這裡加入）"""
    return [("智聯平台（8 館）", fetch_sporetrofit)]


def write_csv(rows: list[dict], now: datetime) -> Path:
    """附加寫入當月的 CSV；檔案不存在時先寫標題列"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"occupancy_{now:%Y-%m}.csv"
    is_new = not path.exists()
    # utf-8-sig：用 Excel 直接開啟時中文不會變亂碼
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
    return path


def log_errors(errors: list[str], now: datetime) -> None:
    """把失敗的來源記到 data/errors.log，之後可以檢查網站是否改版或停止服務"""
    if not errors:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "errors.log").open("a", encoding="utf-8") as f:
        for msg in errors:
            f.write(f"{now.isoformat()}\t{msg}\n")


def print_table(rows: list[dict]) -> None:
    """試跑模式下把結果印成表格"""
    print("\n館名 | 設施 | 人數 | 上限 | 使用率")
    print("-" * 44)
    for r in rows:
        if r["people"] is not None and r["capacity"]:
            rate = f"{r['people'] / r['capacity']:.0%}"
        else:
            rate = "-"
        print(f"{r['center']} | {r['facility']} | {r['people']} | {r['capacity']} | {rate}")


def main() -> None:
    parser = argparse.ArgumentParser(description="臺北市運動中心即時人數爬蟲")
    parser.add_argument(
        "--test",
        action="store_true",
        help="試跑：只印出結果並把原始回應存到 debug/，不寫入 CSV",
    )
    args = parser.parse_args()

    now = datetime.now(TZ).replace(microsecond=0)
    session = requests.Session()
    session.headers.update(HEADERS)

    all_rows, errors = [], []
    for label, fetch in all_sources():
        try:
            rows = fetch(session, args.test)
            all_rows.extend(rows)
            print(f"[成功] {label}：{len(rows)} 筆")
        except Exception as exc:
            errors.append(f"{label}：{type(exc).__name__}: {exc}")
            print(f"[失敗] {label}：{type(exc).__name__}: {exc}")

    for row in all_rows:
        row["timestamp"] = now.isoformat()

    if args.test:
        print_table(all_rows)
        print(f"\n原始回應已存到：{DEBUG_DIR}")
    else:
        if all_rows:
            path = write_csv(all_rows, now)
            print(f"已寫入 {len(all_rows)} 筆 -> {path}")
        log_errors(errors, now)

    # 抓取失敗時回傳錯誤碼，讓 GitHub Actions 亮紅燈提醒；錯誤原因同時記在 errors.log
    if not all_rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
