import os
import time
import json
import requests
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# API 的場地名稱可能是「松0高中」或「西松0中」，不一定包含完整地名。
TARGET_LOCATION_KEYWORDS = ("松",)
MAX_EVENT_FETCH_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 1

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
            time.sleep(0.001)  # 最後 2 秒進入高頻迴圈

def run():
    # 1. 動態計算 6 天後的目標日期
    target_date_dt = datetime.now() + timedelta(days=6)
    target_date_iso = target_date_dt.strftime("%Y-%m-%d")
    print(f"🎯 計算目標預約日期（6天後）：{target_date_iso}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # 優先從 GitHub Secrets (環境變數) 讀取；若沒有則讀取本地 auth.json
        auth_env = os.environ.get("AUTH_JSON_CONTENT")
        if auth_env:
            print("🔑 使用 GitHub Secrets 進行身分驗證")
            auth_data = json.loads(auth_env)
            context = browser.new_context(storage_state=auth_data)
        elif os.path.exists("auth.json"):
            print("🔑 使用本地 auth.json 進行身分驗證")
            context = browser.new_context(storage_state="auth.json")
        else:
            raise FileNotFoundError("❌ 找不到認證資料！請設定 AUTH_JSON_CONTENT 或提供 auth.json 檔案。")
        
        # 2. 提取 Session Cookies 轉給 requests
        playwright_cookies = context.cookies()
        session = requests.Session()
        for cookie in playwright_cookies:
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://dinkup.club",
            "Referer": "https://dinkup.club/xinyi"
        }

        # 3. 精準倒數至 12:00:00
        wait_until_target_time(12, 0, 0)

        # 4. 極速輪詢撈取 Event ID 與 Division ID
        events_url = f"https://dinkup.club/api/events?club=xinyi&date={target_date_iso}"
        target_registrations = []

        print("🔄 正在向 API 獲取開放場次...")
        for attempt in range(1, MAX_EVENT_FETCH_ATTEMPTS + 1):
            try:
                res = session.get(events_url, headers=headers, timeout=2)
                if attempt == 1 or attempt == MAX_EVENT_FETCH_ATTEMPTS:
                    print(f"🔎 活動清單 GET 第 {attempt}/{MAX_EVENT_FETCH_ATTEMPTS} 次：HTTP {res.status_code}")
                if res.status_code == 200:
                    data = res.json()
                    events = data if isinstance(data, list) else data.get("events", [])
                    matching_events = 0
                    target_registrations = []
                    
                    if len(events) > 0:
                        for ev in events:
                            event_title = str(ev.get("title", ""))
                            event_location = str(ev.get("location", ""))

                            # 只比對場地欄位，避免其他活動在標題備註「松山站旁」而誤命中。
                            if not any(keyword in event_location for keyword in TARGET_LOCATION_KEYWORDS):
                                continue

                            matching_events += 1
                            divisions = ev.get("divisions", [])

                            for div in divisions:
                                # 必須是有實際球場的 fun，避免選到前台不顯示的隱藏分組。
                                if (
                                    str(div.get("level", "")).lower() == "fun"
                                    and int(div.get("courtCount", 0) or 0) > 0
                                ):
                                    target_registrations.append({
                                        "event_id": ev.get("id"),
                                        "division_id": div.get("id"),
                                        "title": event_title,
                                        "location": event_location,
                                    })
                                    print(f"📍 找到目標場地：{event_title} | {event_location}")
                                    break
                                elif str(div.get("level", "")).lower() == "fun":
                                    print(
                                        f"⏭️ 跳過前台未顯示的 fun：{event_title} | "
                                        f"courtCount={div.get('courtCount', 0)}"
                                    )
                        
                        if target_registrations:
                            print(f"🔥 [第 {attempt} 次嘗試] 找到 {len(target_registrations)} 個符合的歡樂場次。")
                            break
                        elif attempt == 1 or attempt % 5 == 0:
                            print(f"ℹ️ 共 {len(events)} 個場次，符合松山/西松場地 {matching_events} 個，但尚未找到 fun 分組。")
            except requests.RequestException as exc:
                print(f"⚠️ API 第 {attempt} 次連線失敗：{exc}")
            except Exception:
                print(f"⚠️ API 第 {attempt} 次回應格式無法處理。")

            if attempt < MAX_EVENT_FETCH_ATTEMPTS:
                time.sleep(API_RETRY_DELAY_SECONDS)

        # 5. 逐一報名所有符合條件的歡樂場次 (+2 人)
        if target_registrations:
            payload = {
                "displayName": "Victor",
                "needsPaddle": False,
                "count": 2
            }

            for index, registration in enumerate(target_registrations, start=1):
                event_id = registration["event_id"]
                division_id = registration["division_id"]
                register_url = f"https://dinkup.club/api/events/{event_id}/registrations?club=xinyi"
                registration_payload = {**payload, "divisionId": division_id}

                print(
                    f"⚡ 正在報名第 {index}/{len(target_registrations)} 場："
                    f"{registration['title']} | {registration['location']}"
                )
                try:
                    reg_res = session.post(
                        register_url,
                        json=registration_payload,
                        headers=headers,
                        timeout=5,
                    )
                    print(f"📩 回應狀態碼：{reg_res.status_code} | {reg_res.text}")

                    if reg_res.status_code in [200, 201]:
                        print("✅ 報名成功")
                    else:
                        print(f"❌ 報名失敗，回應碼: {reg_res.status_code}")
                except requests.RequestException as exc:
                    print(f"❌ 報名請求失敗：{exc}")
        else:
            print("\n❌ 未找到符合松山/西松場地的歡樂分組。")

        browser.close()

if __name__ == "__main__":
    run()
