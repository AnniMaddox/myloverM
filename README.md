# 🧠 AI Memory Gateway — myloverM

**讓 AI 擁有長期記憶的輕量轉發網關。**

在你和 LLM 之間加一層記憶系統，讓 M 跨視窗、跨對話都記得你說過的事。

---

## ✨ 功能

- **自定義人設** — `system_prompt.txt` 每次對話自動注入
- **三層記憶系統** — ephemeral（短期）/ stable（穩定）/ evergreen（長期），自動升降級
- **Open Loops** — 追蹤未解決的事，下次聊自動提醒
- **Session 摘要** — 閒置 30 分鐘後自動壓縮對話，讓記憶跨越視窗
- **專屬聊天前端** — 部署在 `docs/m/`，手機友善
- **記憶室 APP** — Our-love 主頁可進入記憶管理介面

---

## 🏗️ 每次送出去的東西

每一輪對話送給 AI 的完整內容：

```
[system 訊息]
  system_prompt.txt 全文
  + 【核心長期記憶】evergreen，最多 MAX_EVERGREEN_INJECT 條（預設 12）
  + 【相關穩定記憶】stable，語意搜尋後最相關 MAX_MEMORIES_INJECT 條（預設 8）
  + 【近期摘要】最近 MAX_SUMMARIES_INJECT 份 session 摘要（預設 2）
  + 【Open Loops】最多 MAX_OPEN_LOOPS_INJECT 個未解決事項（預設 8）
  + 【近期短期狀態】ephemeral 最新 MAX_EPHEMERAL_INJECT 條（預設 8）

[對話歷史]
  前端送來的 N 輪對話（可在聊天介面設定截斷輪數）
```

M 每次回覆後，後端會額外發一次 API 呼叫給記憶萃取模型，分析「有沒有值得存的事」，這次只帶那幾輪對話（不含 system），所以很輕。

---

## 🏗️ 架構

```
使用者（瀏覽器 docs/m/）
        ↓ POST /v1/chat/completions
   AI Memory Gateway（Railway）
   ├── 從 DB 搜尋相關記憶
   ├── 拼裝 system prompt（人設 + 記憶 + open loops + 摘要）
   ├── 轉發請求 → OpenAI / OpenRouter
   └── 回覆後非同步：提取記憶 + 存入 DB + 處理 stale sessions
        ↓
   PostgreSQL（Railway）
        ↓
   docs/m/（GitHub Pages）← 前端
   Our-love 記憶室 APP ← 記憶管理
```

---

## 🚀 部署（Railway）

### 第一步：基本部署

1. Fork 此 repo 到你的 GitHub（建議設 Private）
2. 到 [Railway](https://railway.app) 新增服務 → 連接 GitHub repo
3. 設定環境變數：

| 環境變數 | 說明 | 範例 |
|---------|------|------|
| `API_KEY` | 你的 LLM API Key | `sk-xxxx` |
| `API_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1/chat/completions` |
| `DEFAULT_MODEL` | 預設模型 | `gpt-4o-2024-11-20` |
| `PORT` | 端口 | `8080` |

4. 部署完訪問 `https://你的服務.up.railway.app/`，看到 `{"status":"running"}` 就成功

### 第二步：開啟記憶系統

在 Railway 新增 PostgreSQL 服務，然後加環境變數：

| 環境變數 | 說明 | 預設值 |
|---------|------|--------|
| `DATABASE_URL` | PostgreSQL 連接字串 | — |
| `MEMORY_ENABLED` | 開啟記憶 | `false` |
| `MEMORY_MODEL` | 記憶萃取用的模型（推薦小模型省成本） | `gpt-4o-mini` |
| `MAX_MEMORIES_INJECT` | stable 記憶最多注入幾條 | `8` |
| `MAX_EVERGREEN_INJECT` | evergreen 記憶最多注入幾條 | `12` |
| `MAX_EPHEMERAL_INJECT` | ephemeral 記憶最多注入幾條 | `8` |
| `MAX_SUMMARIES_INJECT` | session 摘要最多注入幾份 | `2` |
| `MAX_OPEN_LOOPS_INJECT` | open loops 最多注入幾條 | `8` |
| `MEMORY_EXTRACT_INTERVAL` | 幾輪提取一次記憶（0=停用，1=每輪，N=每N輪） | `1` |
| `TIMEZONE_HOURS` | 時區偏移小時（台灣填 8） | `8` |
| `CORS_ORIGINS` | 允許的前端來源，逗號分隔 | — |

---

## 🧠 三層記憶系統

| 層級 | 說明 | 注入方式 |
|------|------|---------|
| **ephemeral**（短期） | 最近提取的狀態、事件 | 直接取最新 N 條 |
| **stable**（穩定） | 重複出現或手動升級的記憶 | 語意搜尋，取最相關 N 條 |
| **evergreen**（長期） | 核心關係事實，手動審核確認 | 全取，優先注入 |

**升級路徑：** ephemeral → stable → evergreen（需人工確認）

**鎖定（manual_locked）：** 鎖定的記憶不會被自動覆蓋或清除。

**Open Loops：** AI 偵測到「未解決的事」自動建立，resolved 或 dropped 後不再注入。

**Session 摘要：** 閒置 30 分鐘後，後端自動把那段對話壓縮成摘要存入 DB，下次對話注入。

---

## 📱 聊天前端（docs/m/）

部署在 GitHub Pages 的 `docs/m/`，手機友善。

**設定方式：** 點右上角 ⋯ → System tab
- **後端 URL**：填 Railway 根網址（不含 /v1）
- **Model**：留空用後端預設，或手動填
- **上下文輪數**：0=不截斷，設 N 則每次只送最近 N 輪給 M（超出的靠記憶補充）

---

## 📁 檔案說明

```
myloverM/
├── main.py                 # 網關主程序（API endpoints + chat 處理）
├── database.py             # 資料庫操作（PostgreSQL，含 init_tables）
├── memory_extractor.py     # AI 記憶萃取
├── system_prompt.txt       # M 的人設（自行編輯）
├── requirements.txt        # Python 依賴
├── Dockerfile              # 容器配置
├── frontend/               # 聊天前端（React + Vite）
│   └── src/
│       ├── components/
│       │   ├── ChatWindow.tsx   # 主要聊天視窗（含截斷邏輯）
│       │   ├── SidePanel.tsx    # 右側設定面板
│       │   ├── ChatSidebar.tsx  # 左側對話列表
│       │   └── MessageList.tsx  # 訊息氣泡
│       ├── api.ts          # 後端 API 呼叫集中處
│       ├── session.ts      # Session 管理（30min timeout）
│       └── types.ts        # TypeScript 型別定義
└── README.md
```

---

## 🔧 API 端點

| 路徑 | 方法 | 說明 |
|------|------|------|
| `/` | GET | 健康檢查，查看網關狀態 |
| `/v1/chat/completions` | POST | 主聊天介面（OpenAI 格式） |
| `/v1/models` | GET | 取得後端 model 名稱 |
| `/api/memories` | GET | 列出記憶（支援 ?search= ?tier= ?status=） |
| `/api/memories/{id}/upgrade` | POST | 升級記憶層級 |
| `/api/memories/{id}/lock` | POST | 切換鎖定狀態 |
| `/api/memories/{id}` | DELETE | 刪除記憶 |
| `/api/open-loops` | GET | 列出 open loops（?status=open\|all） |
| `/api/open-loops/{id}` | PATCH | 更新 loop 狀態（resolved/dropped/open） |
| `/api/summaries` | GET | 列出 session 摘要（?limit=50） |
| `/export/memories` | GET | 匯出所有記憶 JSON（備份用） |
| `/import/memories` | GET/POST | 記憶匯入頁面（純文字 / JSON 備份還原） |

---

## 💾 localStorage 鍵值（前端）

| 鍵 | 說明 |
|----|------|
| `myloverM-api-base-url` | 後端 Railway URL |
| `myloverM-model` | 指定模型（留空用後端預設） |
| `myloverM-context-turns` | 上下文截斷輪數（0=不截斷） |
| `myloverM_chats` | 對話紀錄（可在記憶室匯出備份） |

---

## ❓ 常見問題

**Q: 部署後訪問 502？**
A: 確認 `PORT=8080`，Railway 預設用 8080。

**Q: CORS 錯誤？**
A: 在 Railway 加 `CORS_ORIGINS=https://你的前端網域` 環境變數。

**Q: 記憶越來越多，token 會爆嗎？**
A: 每次最多注入固定條數（預設合計約 36 條，~3200 token），不會無限增長。

**Q: 上下文截斷後 M 會失憶嗎？**
A: 不會完全失憶。每輪萃取的 ephemeral 記憶持續存入 DB，舊對話會被摘要，都會在下次注入。截斷主要是省 token 用的。

**Q: 怎麼備份？**
A: 記憶：`/export/memories` 下載 JSON。對話紀錄：記憶室 APP → 備份 tab → 匯出對話。

---

## 📋 更新日誌

### v3.0（2026-03-08）

- **前端聊天介面** — 部署在 `docs/m/`，含左側列表、右側設定面板、streaming
- **上下文截斷** — 聊天前端可設定最多送幾輪對話，省 token
- **記憶室 APP** — Our-love 主頁新增記憶管理 APP，可管理記憶/待審/Loops/備份
- **新增 API** — `/api/memories`、`/api/open-loops`、`/api/summaries`（支援記憶室）
- **匯出修正** — `/export/memories` 加 `Content-Disposition: attachment` header
- **回傳按鈕** — 聊天頁加 ‹ 返回鍵，可回到 Our-love 主頁
- **測試頁連結** — System 面板加入測試頁快速入口

### v2.0（2026-03-01）

- **三層記憶架構** — ephemeral / stable / evergreen 分層管理
- **Open Loops** — 未解決事項自動追蹤
- **Session 摘要** — 閒置 30 分鐘自動摘要
- **記憶提取間隔** — `MEMORY_EXTRACT_INTERVAL` 環境變數
- **完整上下文提取** — 記憶萃取帶入最近幾輪對話，不只看最後一輪

### v1.0（2026-02-26）

- 初始版本：自定義人設、長期記憶、預置記憶匯入
- 支援 OpenRouter / OpenAI 等 LLM 服務商

---

*Built with love by 七堂伽蓝_ & Midsummer (Claude Sonnet 4.6)*
