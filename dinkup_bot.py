import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from threading import Lock

import requests
from playwright.sync_api import sync_playwright

# API 的場地名稱可能是「松0高中」或「西松0中」，不一定包含完整地名。
TARGET_LOCATION_KEYWORDS = ("松",)
MAX_EVENT_FETCH_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 1
MAX_DATE_WORKERS = 3


def get_target_day_offsets():
    """預設查詢 5、6 天後；拒絕空值、非整數與非未來日期。"""
    raw = os.environ.get("TARGET_DAY_OFFSETS", "5,6")
    try:
        offsets = tuple(dict.fromkeys(int(value.strip()) for value in raw.split(",")))
        if any(offset < 1 or offset > 365 for offset in offsets):
            raise ValueError
    except ValueError as exc:
        raise ValueError("TARGET_DAY_OFFSETS 必須是 1～365 的逗號分隔整數，例如 5,6") from exc
    return offsets


def get_target_dates(day_offsets, now=None):
    today = (now or datetime.now()).date()
    return [(today + timedelta(days=offset)).isoformat() for offset in day_offsets]


def wait_until_target_time(target_hour=12, target_minute=0, target_second=0):
    """毫秒級倒數等待至 12:00:00"""
    now = datetime.now()
    target_time = now.replace(hour=target_hour, minute=target_minute, second=target_second, microsecond=0)

    if now > target_time:
        print(f"⏰ 當前時間 {now.strftime('%H:%M:%S')} 已超過開搶時間，直接啟動！")
        return

    print(f"⏳ 當前時間：{now.strftime('%H:%M:%S')}，毫秒級預熱倒數中...")
    while True:
        now = datetime.now()
        remaining = (target_time - now).total_seconds()
        if remaining <= 0:
            print("\n🚀 12:00:00 到達！啟動 API 搶報！")
            break
        elif remaining > 2:
            time.sleep(0.5)
        else:
            time.sleep(0.001)


def select_registrations(data):
    events = data if isinstance(data, list) else data.get("events", [])
    if not isinstance(events, list):
        raise ValueError("活動清單不是陣列")
    registrations = []
    for event in events:
        try:
            location = str(event.get("location", ""))
            if not any(keyword in location for keyword in TARGET_LOCATION_KEYWORDS):
                continue
            if event.get("id") is None:
                continue
            for division in event.get("divisions", []):
                if (
                    str(division.get("level", "")).lower() == "fun"
                    and int(division.get("courtCount", 0) or 0) > 0
                    and division.get("id") is not None
                ):
                    registrations.append({
                        "event_id": event["id"],
                        "division_id": division["id"],
                        "title": str(event.get("title", "")),
                        "location": location,
                    })
                    break
        except (AttributeError, TypeError, ValueError):
            print("⚠️ 跳過格式異常的活動。")
    return registrations


def register_once(session, headers, registration, attempted, lock, target_date):
    key = (registration["event_id"], registration["division_id"])
    # 送出前就標記；即使逾時或回應失敗，也不盲目重送有副作用的 POST。
    with lock:
        if key in attempted:
            return
        attempted.add(key)

    payload = {
        "displayName": "Victor",
        "needsPaddle": False,
        "count": 2,
        "divisionId": registration["division_id"],
    }
    url = f"https://dinkup.club/api/events/{registration['event_id']}/registrations?club=xinyi"
    print(f"⚡ [{target_date}] 報名：{registration['title']} | {registration['location']}")
    try:
        response = session.post(url, json=payload, headers=headers, timeout=5)
        if response.status_code in (200, 201):
            print(f"✅ [{target_date}] 報名成功：{registration['title']}")
        else:
            print(f"❌ [{target_date}] 報名失敗：HTTP {response.status_code} | {response.text}")
    except requests.RequestException as exc:
        print(f"⚠️ [{target_date}] 報名結果不明，不自動重送，請確認網站報名狀態：{exc}")


def poll_date(target_date, cookies, headers, attempted, lock):
    # 每個日期使用自己的 Session，避免執行緒共用可變的 cookie / 連線狀態。
    with requests.Session() as session:
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"]
            )
        events_url = f"https://dinkup.club/api/events?club=xinyi&date={target_date}"
        for attempt in range(1, MAX_EVENT_FETCH_ATTEMPTS + 1):
            registrations = []
            try:
                response = session.get(events_url, headers=headers, timeout=2)
                print(f"🔎 [{target_date}] GET {attempt}/{MAX_EVENT_FETCH_ATTEMPTS}：HTTP {response.status_code}")
                if response.status_code == 200:
                    registrations = select_registrations(response.json())
            except requests.RequestException as exc:
                print(f"⚠️ [{target_date}] 查詢失敗：{exc}")
            except (AttributeError, TypeError, ValueError):
                print(f"⚠️ [{target_date}] API 回應格式無法處理。")

            # 找到就報名，不等待其他日期；完成報名後仍查詢，捕捉同日稍晚上架的場次。
            for registration in registrations:
                register_once(session, headers, registration, attempted, lock, target_date)
            if attempt < MAX_EVENT_FETCH_ATTEMPTS:
                time.sleep(API_RETRY_DELAY_SECONDS)


def poll_dates(target_dates, cookies, headers):
    target_dates = list(dict.fromkeys(target_dates))
    attempted = set()
    lock = Lock()
    if not target_dates:
        return attempted
    with ThreadPoolExecutor(max_workers=min(MAX_DATE_WORKERS, len(target_dates))) as executor:
        futures = [
            executor.submit(poll_date, target_date, cookies, headers, attempted, lock)
            for target_date in target_dates
        ]
        for future in as_completed(futures):
            future.result()
    if not attempted:
        print("❌ 未找到符合松山/西松場地的歡樂分組。")
    return attempted


def run():
    day_offsets = get_target_day_offsets()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            auth_env = os.environ.get("AUTH_JSON_CONTENT")
            if auth_env:
                print("🔑 使用 GitHub Secrets 進行身分驗證")
                context = browser.new_context(storage_state=json.loads(auth_env))
            elif os.path.exists("auth.json"):
                print("🔑 使用本地 auth.json 進行身分驗證")
                context = browser.new_context(storage_state="auth.json")
            else:
                raise FileNotFoundError("❌ 找不到認證資料！請設定 AUTH_JSON_CONTENT 或提供 auth.json 檔案。")

            cookies = context.cookies()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
                "Origin": "https://dinkup.club",
                "Referer": "https://dinkup.club/xinyi",
            }
            wait_until_target_time(12, 0, 0)
            target_dates = get_target_dates(day_offsets)
            print(f"🎯 目標預約日期：{', '.join(target_dates)}")
            poll_dates(target_dates, cookies, headers)
        finally:
            browser.close()


if __name__ == "__main__":
    run()
