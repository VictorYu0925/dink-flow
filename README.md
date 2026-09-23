# DinkUp Bot

使用 Python 3.11、Requests 與 Playwright，自動查詢松山／西松場地並報名兩人。每個活動優先選競技（`competitive`），沒有有效競技分組才選歡樂（`fun`）；分組皆須有 ID 且 `courtCount > 0`。

## 競技失敗時改報歡樂

- 若查詢資料提供 `capacity` 與 `confirmedCount`（或 `confirmed` 名單），且競技正取名額不足兩人，有歡樂分組時直接選歡樂。資料缺少或格式異常時不猜測剩餘名額。
- 競技 POST 回傳 HTTP 400、403、404、409 或 422，且 JSON 含非空 `error`，會使用同次 GET 快取的歡樂分組改報一次，不增加 GET。錯誤包含已報名、重複或候補等訊息時不改報。
- HTTP 200／201 視為已受理，可能是正取或候補，不自動取消或另報一組。逾時、斷線、5xx、401、408、429 或無法辨識的回應不改報，以免留下重複報名。
- 沒有可用的歡樂分組就不改報；歡樂失敗後也不再重送。名額可能在 GET 與 POST 間變動，無法保證不進入候補。

## 安裝與執行

```sh
python -m pip install pipenv
pipenv sync
pipenv run playwright install chromium
pipenv run python save_session.py
pipenv run python dinkup_bot.py
```

`save_session.py` 需要本機安裝 Google Chrome；登入後儲存的 `auth.json` 不可提交。也可使用 `AUTH_JSON_CONTENT` 環境變數提供登入狀態，其優先於本機檔案。

## 查詢日期與效能

預設 `TARGET_DAY_OFFSETS=5,6`：例如 2026/9/22 執行，會查詢 9/27 與 9/28。可用逗號分隔的正整數調整（1～365，重複值會移除）。PowerShell 範例：

```powershell
$env:TARGET_DAY_OFFSETS = "5,6"
pipenv run python dinkup_bot.py
```

GitHub Actions 兩個工作流程都讀取 repository variable `TARGET_DAY_OFFSETS`，未設定則使用 `5,6`。需要查詢未來一週時，設為 `1,2,3,4,5,6,7`。

- 開搶前 30 秒觸發一次背景預查，各日期各 GET 一次；倒數迴圈不等待網路結果，也不會反覆觸發預查。
- 中午發出開搶訊號，已取得的場次立即 POST；GET 尚未完成的日期收到結果後再 POST，不阻擋其他日期，也不會提前報名。
- 預查有場次時，首批 POST 完成後間隔一秒，再補查一次以捕捉新增場次。預設兩天正常共四次 GET。
- 預查失敗或無場次時，中午立即補查，並在該輪完成後間隔一秒再查一次，每日最多三次 GET，兩天最多六次。中午後才啟動或錯過預查窗口時，沿用原本三輪查詢流程。
- 最多同時處理三個日期，各自使用獨立 HTTP Session，預查和 POST 沿用相同 Session。
- GET 使用分開的連線／讀取 timeout：預查為 `(5, 15)` 秒，中午後查詢為 `(5, 10)` 秒；回應提早到達即處理，不會固定等滿。這不是整個請求的總時限。POST timeout 仍為五秒。超過三個日期時，其他日期會排隊處理。
- 查詢日誌包含開始時間、輪次、HTTP 狀態或錯誤類型與耗時。若某日期所有查詢都失敗，會在其他日期處理完後以錯誤結束，讓 Actions 標示失敗；不再誤報為查無場次。此時其他日期可能已完成報名，重跑前應確認報名狀態。
- 當次執行記錄 `(event_id, division_id)`，同一活動僅啟動一次報名流程，最多競技一次及明確拒絕後的歡樂一次；補查或並行查詢不會再次啟動。逾時請到網站確認結果。

## 限制與測試

查詢只涵蓋設定日期與上述有限查詢期間；更晚上架的活動仍可能漏掉。提前 30 秒預查提供更多網路等待餘裕，但不保證 GET 在中午前完成；日期超過三個時，排隊中的日期可能要到中午後才開始查詢。目前沒有核實 API 的開放報名狀態欄位，因此只依場地、競技／歡樂優先序與 `courtCount > 0` 篩選，不能保證活動在中午已可報名。去重不跨程式執行；重跑或擴大查詢範圍前，請確認已有報名。

本機使用系統時間等待中午；Actions 使用 `Asia/Taipei`，排程 cron 使用 UTC。`dry-run.yml` 也會真實報名。

```sh
pipenv run python -m unittest discover -s tests -v
pipenv run python -m py_compile dinkup_bot.py save_session.py
```

測試模擬 HTTP 與等待，不會登入或送出真實報名。
