import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from threading import Event, Lock

import requests
from playwright.sync_api import sync_playwright

# API 的場地名稱可能是「松0高中」或「西松0中」，不一定包含完整地名。
TARGET_LOCATION_KEYWORDS = ("松",)
DIVISION_PRIORITY = ("competitive", "fun")
DIVISION_LABELS = {"competitive": "競技", "fun": "歡樂"}
REGISTRATION_COUNT = 2
MAX_EVENT_FETCH_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 1
MAX_DATE_WORKERS = 3
PREFETCH_SECONDS = 30
PREFETCH_GET_TIMEOUT = (5, 15)  # 連線、讀取等待秒數；不是整個請求的總時限。
EVENT_GET_TIMEOUT = (5, 10)


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


def wait_until_target_time(target_hour=12, target_minute=0, target_second=0, on_prefetch=None):
    """毫秒級倒數等待至 12:00:00"""
    now = datetime.now()
    target_time = now.replace(hour=target_hour, minute=target_minute, second=target_second, microsecond=0)

    if now > target_time:
        print(f"⏰ 當前時間 {now.strftime('%H:%M:%S')} 已超過開搶時間，直接啟動！")
        return

    print(f"⏳ 當前時間：{now.strftime('%H:%M:%S')}，毫秒級預熱倒數中...")
    prefetch_started = False
    while True:
        now = datetime.now()
        remaining = (target_time - now).total_seconds()
        if remaining <= 0:
            print("\n🚀 12:00:00 到達！啟動 API 搶報！")
            break
        if remaining <= PREFETCH_SECONDS and not prefetch_started and on_prefetch is not None:
            prefetch_started = True
            on_prefetch()  # 只提交背景工作，不等待 GET 結果。
        if remaining > 2:
            time.sleep(0.5)
        else:
            time.sleep(0.001)


def lacks_confirmed_places(division):
    """只有容量及人數皆可核實時，才判斷正取名額不足。"""
    try:
        capacity = int(division["capacity"])
        if division.get("confirmedCount") is not None:
            confirmed = int(division["confirmedCount"])
        elif isinstance(division.get("confirmed"), list):
            confirmed = len(division["confirmed"])
        else:
            return False
        return capacity >= 0 and confirmed >= 0 and capacity - confirmed < REGISTRATION_COUNT
    except (KeyError, TypeError, ValueError):
        return False


def select_registrations(data):
    events = data if isinstance(data, list) else data["events"]
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
            candidates = {}
            for division in event.get("divisions", []):
                try:
                    level = str(division.get("level", "")).strip().lower()
                    if (
                        level in DIVISION_PRIORITY
                        and int(division.get("courtCount", 0) or 0) > 0
                        and division.get("id") is not None
                    ):
                        candidates.setdefault(level, division)
                except (AttributeError, TypeError, ValueError):
                    continue
            for level in DIVISION_PRIORITY:
                if level not in candidates:
                    continue
                registration = {
                    "event_id": event["id"],
                    "division_id": candidates[level]["id"],
                    "level": level,
                    "title": str(event.get("title", "")),
                    "location": location,
                }
                if level == "competitive" and "fun" in candidates:
                    fallback = {**registration, "division_id": candidates["fun"]["id"], "level": "fun"}
                    if lacks_confirmed_places(candidates[level]):
                        print(f"↪️ {registration['title']} 競技正取名額不足 {REGISTRATION_COUNT} 人，改選歡樂。")
                        registration = fallback
                    else:
                        registration["fallback"] = fallback
                registrations.append(registration)
                break
        except (AttributeError, TypeError, ValueError):
            print("⚠️ 跳過格式異常的活動。")
    return registrations


def can_fallback_after_rejection(response):
    # 5xx、逾時、非 JSON 回應都不能證明沒有建立報名。
    if response.status_code not in (400, 403, 404, 409, 422):
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, str) or not error.strip():
        return False
    # 已有報名不是改報的依據，避免跨執行或候補造成重複。
    return not any(word in error.lower() for word in (
        "already", "duplicate", "已報名", "已經報名", "已報過", "重複", "候補", "備取", "waitlist",
    ))


def register_once(session, headers, registration, attempted, lock, target_date):
    key = (registration["event_id"], registration["division_id"])
    # 送出前就標記；即使逾時或回應失敗，也不盲目重送有副作用的 POST。
    with lock:
        if any(event_id == registration["event_id"] for event_id, _ in attempted):
            return
        attempted.add(key)

    payload = {
        "displayName": "Victor",
        "needsPaddle": False,
        "count": REGISTRATION_COUNT,
        "divisionId": registration["division_id"],
    }
    url = f"https://dinkup.club/api/events/{registration['event_id']}/registrations?club=xinyi"
    choices = [registration]
    fallback = registration.get("fallback")
    if fallback is not None and fallback["division_id"] != registration["division_id"]:
        choices.append(fallback)
    for index, choice in enumerate(choices):
        if index:
            with lock:
                attempted.add((choice["event_id"], choice["division_id"]))
            print(f"↪️ [{target_date}] 競技明確拒絕，改報歡樂一次。")
        payload["divisionId"] = choice["division_id"]
        division_label = DIVISION_LABELS[choice["level"]]
        print(f"⚡ [{target_date}] 報名{division_label}：{choice['title']} | {choice['location']}")
        try:
            response = session.post(url, json=dict(payload), headers=headers, timeout=5)
            if response.status_code in (200, 201):
                print(f"✅ [{target_date}] {division_label}報名已受理（正取／候補請確認網站）：{choice['title']}")
                return
            print(f"❌ [{target_date}] {division_label}報名失敗：HTTP {response.status_code} | {response.text}")
            if not can_fallback_after_rejection(response):
                print(f"⚠️ [{target_date}] 不符合安全改報條件，請確認網站報名狀態。")
                return
        except requests.RequestException as exc:
            print(f"⚠️ [{target_date}] 報名結果不明，不自動重送或改報，請確認網站報名狀態：{exc}")
            return


def poll_date(target_date, cookies, headers, attempted, lock, start_event=None, cancelled=None):
    # 每個日期使用自己的 Session，避免執行緒共用可變的 cookie / 連線狀態。
    with requests.Session() as session:
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"]
            )
        events_url = f"https://dinkup.club/api/events?club=xinyi&date={target_date}"
        max_attempts = MAX_EVENT_FETCH_ATTEMPTS
        successful_queries = 0
        for attempt in range(1, MAX_EVENT_FETCH_ATTEMPTS + 1):
            registrations = []
            is_prefetch = attempt == 1 and start_event is not None and not start_event.is_set()
            timeout = PREFETCH_GET_TIMEOUT if is_prefetch else EVENT_GET_TIMEOUT
            phase = "預查" if is_prefetch else "查詢"
            started = time.monotonic()
            label = f"[{target_date}] {phase} GET {attempt}/{max_attempts}"
            print(f"🔎 {datetime.now():%H:%M:%S} {label} 開始，connect/read timeout={timeout}")
            try:
                response = session.get(events_url, headers=headers, timeout=timeout)
                print(f"🔎 {label}：HTTP {response.status_code}，耗時 {time.monotonic() - started:.2f}s")
                if response.status_code == 200:
                    registrations = select_registrations(response.json())
                    successful_queries += 1
                    print(f"📋 {label}：符合場次 {len(registrations)} 個")
            except requests.RequestException as exc:
                print(f"⚠️ {label} 耗時 {time.monotonic() - started:.2f}s，{type(exc).__name__}：{exc}")
            except (AttributeError, KeyError, TypeError, ValueError):
                print(f"⚠️ {label} API 回應格式無法處理。")

            if attempt == 1 and start_event is not None:
                # 預查只有一次；結果留在此工作中，沿用同一個 Session 報名。
                if registrations:
                    max_attempts = 2  # 有快取：報名後僅補查一次。
                start_event.wait()
                if cancelled is not None and cancelled.is_set():
                    return

            # 必須等到開搶；慢日期的 GET 不影響其他日期送出 POST。
            for registration in registrations:
                register_once(session, headers, registration, attempted, lock, target_date)
            if attempt >= max_attempts:
                break
            # 預查空值或失敗，中午立即補查，不再額外等待一秒。
            if not (attempt == 1 and start_event is not None and not registrations):
                time.sleep(API_RETRY_DELAY_SECONDS)
        if successful_queries == 0:
            print(f"❌ [{target_date}] 所有查詢均失敗，無法判定是否有可報名場次。")
        elif successful_queries < max_attempts:
            print(f"⚠️ [{target_date}] 僅 {successful_queries}/{max_attempts} 次取得有效資料，查詢結果可能不完整。")
        return successful_queries > 0


def poll_dates(target_dates, cookies, headers, wait_for_noon=False):
    target_dates = list(dict.fromkeys(target_dates))
    attempted = set()
    lock = Lock()
    if not target_dates:
        return attempted
    with ThreadPoolExecutor(max_workers=min(MAX_DATE_WORKERS, len(target_dates))) as executor:
        futures = []
        start_event = Event()
        cancelled = Event()

        def start_prefetch():
            if not futures:
                futures.extend(
                    executor.submit(
                        poll_date, target_date, cookies, headers, attempted, lock,
                        start_event, cancelled,
                    )
                    for target_date in target_dates
                )

        if wait_for_noon:
            try:
                wait_until_target_time(on_prefetch=start_prefetch)
            except BaseException:
                # 中斷倒數時釋放等待中的工作，但禁止提前報名。
                cancelled.set()
                raise
            finally:
                start_event.set()
        if not futures:
            # 晚啟動或時鐘跨過預查窗口：直接走原本三輪查詢流程。
            futures = [
                executor.submit(poll_date, target_date, cookies, headers, attempted, lock)
                for target_date in target_dates
            ]
        future_dates = dict(zip(futures, target_dates))
        failed_dates = []
        for future in as_completed(futures):
            if future.result() is False:
                failed_dates.append(future_dates[future])
    if failed_dates:
        raise RuntimeError(f"以下日期未取得任何有效活動資料，請檢查查詢日誌：{', '.join(sorted(failed_dates))}")
    if not attempted:
        print("❌ 未找到符合松山/西松場地的競技或歡樂分組。")
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
            target_dates = get_target_dates(day_offsets)
            print(f"🎯 目標預約日期：{', '.join(target_dates)}")
            poll_dates(target_dates, cookies, headers, wait_for_noon=True)
        finally:
            browser.close()


if __name__ == "__main__":
    run()
