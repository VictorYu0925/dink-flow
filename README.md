# DinkUp 報名機器人

預設每天台灣時間中午查詢 4、5、6 天後、地點名稱含「松」的活動，依組別 `label` 選擇球敘，排除教練課。
優先報名歡樂場兩位；歡樂正取不足兩位或伺服器明確回覆名額不足時，才改報競技。
沒有歡樂組時可選競技。逾時、登入失效、候補或結果不明時不重送、不改報。
每帳號限一位或 DUPR 限本人組別會跳過；已有正取／候補的活動不重複報名。

## Repository secrets

在 GitHub repository 的 Settings → Secrets and variables → Actions 設定：

| Secret | 用途 |
| --- | --- |
| `AUTH_JSON_CONTENT` | `save_session.py` 重新登入產生的完整 `auth.json` 內容 |
| `NOTIFY_EMAIL` | 成功通知收件地址 |
| `RESEND_API_KEY` | Resend 後台建立的寄信 API Key |
| `RESEND_FROM` | 選填，已驗證網域的寄件地址；預設 `DinkUp <onboarding@resend.dev>` |

透過 [Resend Email API](https://resend.com/docs/api-reference/emails/send-email) 寄信，無須 SMTP 帳號或額外 Python 套件。舊的 `SMTP_*` secrets 不再使用，可移除。
註冊 Resend 後，在 API Keys 建立寄信金鑰，將其填入 `RESEND_API_KEY`，並設定 `NOTIFY_EMAIL`。
預設 `resend.dev` 寄件地址只供測試寄到自己的 Resend 註冊信箱；寄給其他地址須驗證自有網域，並設定 `RESEND_FROM`。[官方限制](https://resend.com/docs/knowledge-base/403-error-resend-dev-domain)
程式在所有日期查詢完成後彙整通知；只通知此次 POST 回應已確認本帳號兩位正取的場次。
候補、只有一位正取或回應缺少可驗證名單時，不寄成功通知，Actions 會標示失敗供人工確認。
寄信失敗也會標示失敗，但不重送報名或盲目重送郵件。API 回傳郵件 ID 代表 Resend 已受理，不保證送達；請到 Resend 後台查詢送達、退信或逾時請求結果。通知不是持續追蹤服務，不會自動偵測之後候補轉正取。

`TARGET_DAY_OFFSETS` 是 repository **variable**，可選填 `4,5,6`；若已設定其他值，會覆蓋程式預設。

## 排查報名失敗

1. 查看 Actions 的 Run DinkUp Bot 日誌。登入檢查若失敗，執行 `save_session.py`，重新登入並更新 `AUTH_JSON_CONTENT`。不要提交 `auth.json`。
2. 確認地點、開放日期與每帳號人數限制。程式不會為繞過每帳號上限而分次報名。
3. HTTP 200/201 僅表示請求受理；必須核對正取名單。報名拒絕或結果不明現在會讓 workflow 失敗。

2026-11-10 唯讀檢查發現：本地登入已無效；11/16 松0高中活動每帳號上限為 1；11/15 歡樂球敘位於永春0中，會被既有地點規則排除。
這些是當日觀察，不能取代 CI log。GitHub 最新執行狀態雖為 success，但未登入下載 log 得到 403，無法確認 CI secret 的登入狀態。

## 驗證

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m py_compile dinkup_bot.py save_session.py
```

測試模擬報名／Resend HTTP 與時間等待，不會真實報名或寄信。`dry-run.yml` 實際會報名，不是測試套件。
