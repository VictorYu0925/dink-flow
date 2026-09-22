# DinkUp Bot

使用 Python 3.11、Requests 與 Playwright，自動查詢松山／西松場地的 `fun` 分組並報名兩人。

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

- 最多同時處理三個日期，各自使用獨立 HTTP Session；找到符合場次就送出報名，不等待其他日期。
- 每個日期最多查詢三輪，輪次間隔一秒；即使已找到場次，也繼續捕捉同日稍晚上架的其他場次。預設兩天共六次 GET，一週則為二十一次。
- GET 逾時為兩秒，POST 為五秒；實際耗時還包含網路與報名時間。超過三個日期時，其他日期會排隊處理。
- 當次執行以 `(event_id, division_id)` 去重；成功、拒絕或逾時的 POST 都不自動重送。逾時請到網站確認結果。

## 限制與測試

查詢只涵蓋設定的日期與三輪查詢期間；更晚上架的活動仍可能漏掉。目前沒有核實 API 的開放報名狀態欄位，因此沿用場地、`fun`、`courtCount > 0` 篩選，不能保證提前顯示的活動已可報名。去重不跨程式執行；重跑或擴大查詢範圍前，請確認已有報名。

本機使用系統時間等待中午；Actions 使用 `Asia/Taipei`，排程 cron 使用 UTC。`dry-run.yml` 也會真實報名。

```sh
pipenv run python -m unittest discover -s tests -v
pipenv run python -m py_compile dinkup_bot.py save_session.py
```

測試模擬 HTTP 與等待，不會登入或送出真實報名。
