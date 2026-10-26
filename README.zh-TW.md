# anki-telegram

[English](README.md) | 繁體中文

用 Telegram 傳日文單字或句子，自動查 JMdict、翻譯、產生語音並寫入 Anki。

## 功能

- 傳單字或整句日文：以 fugashi（unidic-lite）斷詞、還原辭書形，查 [JMdict](https://www.edrdg.org/jmdict/j_jmdict.html)，扣除 Anki 裡已有的字後，每個新字各送一則預覽。
- 預覽內容：字（讀音）、前 3 個意思（翻譯＋英文原文）、Tatoeba 例句與翻譯；來自句子時附原句（目標字粗體）與原句翻譯。
- 按鈕：💾 Save 寫入 Anki（可選 edge-tts 語音）、🗑 Discard 捨棄、↩️ Undo 刪除剛存的卡。
- 透過自架 Anki sync server 同步（`ANKI_SYNC_ENDPOINT` 必填；debounce 2 小時，每次新增／刪除都重新計時；可用 `/sync` 立即同步；距上次同步超過 72 小時時，處理訊息前會先做一次資料庫同步），手機與桌面 Anki sync 後就看得到新卡。
- 只回應白名單中的一個 Telegram 帳號。

## 需求

- Docker（含 compose plugin），x86_64 已驗證。
- 自架 Anki sync server（例如 `python -m anki.syncserver`，見 [Anki 手冊](https://docs.ankiweb.net/sync-server.html)），手機／桌面 Anki 也同步到同一台，且桌面 Anki 已建立 note type 與 deck（見「初始化」）。
- Telegram bot token（向 [@BotFather](https://t.me/BotFather) 申請）與自己的 Telegram user id。
- 可選：Gemini 或 DeepL（未實測）API key（預設翻譯成英文，也可改成繁體中文等其他語言；不設定則只顯示英文釋義）。

## 使用方式

| 輸入 | 說明 |
| --- | --- |
| 日文單字或句子 | 每個新字一則預覽；沒有新字時回覆「沒有新字」，查不到的字另外列出 |
| `/sync` | 立即與 sync server 同步 |
| `/stats` | 今日新增、deck 總卡數、上次同步時間 |
| `/help` | 說明 |

預覽在 `PENDING_TTL_HOURS`（預設 24 小時）後過期；Undo 不受此限制，bot 重啟後也可以按。

bot 訊息預設為英文；本文引用的是 `BOT_LANGUAGE=zh-Hant` 的訊息。繁體中文介面與翻譯請在 `.env` 設定 `BOT_LANGUAGE=zh-Hant`、`TRANSLATION_TARGET=ZH-HANT`（見「介面語言與文字」）。

## 快速開始

1. 依「初始化」第 1 步在桌面 Anki 建立 note type 與 deck，並 sync 到 sync server。
2. 依「部署」準備 `.env` 與 `data/`，執行 bootstrap，再 `docker compose up -d --build`。
3. 在 Telegram 對 bot 傳「食べた」測試。

## 開發環境（devcontainer）

1. 複製 `.env.example` 為 `.env.dev` 並填入測試用的值（測試 bot token、sync server 帳密；使用 local-sync 時 `ANKI_SYNC_ENDPOINT=http://anki-sync:8080/`）。
2. 用 VS Code「Reopen in Container」開啟。容器內使用者為 `app`（uid 1000），`/data` 是 named volume。
3. 常用指令：

| 指令 | 說明 |
| --- | --- |
| `make test` | 單元測試（跳過 `integration`） |
| `make test-integration` | 整合測試（需先啟動 local-sync） |
| `make lint` / `make format` | ruff |
| `make verify-sync` | 同步 API 驗證，對 `ANKI_SYNC_ENDPOINT`（請指向 local-sync；需空的 server 帳號） |
| `make verify-sync-readonly` | 同步 API 驗證，對 `ANKI_SYNC_ENDPOINT`（唯讀：登入、full download、無變更 sync） |
| `make run` | 啟動 bot |
| `make bootstrap` | 首次初始化 |
| `make build-tatoeba` | 下載 Tatoeba 並建立例句資料庫 `$DATA_DIR/tatoeba.db` |

### local-sync（本地 sync server）

在**主機**上執行（devcontainer 內沒有 docker）：

```sh
make local-sync-up      # 啟動 anki-sync（帳密取自 SYNC_USER1，預設 user:pass）
make local-sync-reset   # 清空 server 資料後重新啟動（`make verify-sync` 前需要）
```

容器內以 `http://anki-sync:8080/` 連線。

## 初始化（bootstrap）

1. 在桌面 Anki 建立 note type `JP Bot Vocab`（欄位 `Word`、`Reading`、`Meaning`、`MeaningTL`、`Sentence`、`SentenceTL`、`ExampleJP`、`ExampleTL`、`Audio`、`Source`，可額外加欄位、順序不限）與 deck（預設 `Japanese::Bot`），sync 到 sync server，並先備份。卡片模板（Front Template / Back Template / Styling）分別貼上 `templates/jp-bot-vocab/` 的 `front.html`、`back.html`、`style.css`；之後修改模板的 HTML / CSS 不會觸發 full sync，但新增、刪除、改名、調整順序欄位，或新增、刪除卡片類型都會。
2. bot 停止狀態下執行（先 dry-run 檢視，再加 `--yes`）：
   - 首次初始化：`docker compose run --rm bot python -m ankibot.bootstrap`。本地 collection 非空時拒絕執行。
   - full sync 後復原：`docker compose run --rm bot python -m ankibot.bootstrap --force`。會列出尚未 sync 的本地變更（新增／修改列出 `Word`、`Reading`，刪除只列筆數），這些變更下載後會遺失。舊的 `collection.anki2`（含 `-wal`、`-shm`）與 `collection.media.db2` 改名為 `backup-<時間戳>-<原檔名>` 保留，媒體目錄不動。成功後刪除 `full_sync_required` 標記檔。
3. 下載後會檢查 note type、必要欄位、deck 與 `DEDUP_EXTRA_SOURCES`，不符直接報錯，不會自動建立。

`DEDUP_EXTRA_SOURCES` 未設定 `ReadingField` 時只以表記比對，假名表記可能誤中同音詞（例：存「かみ」會讓 紙／神／髪 都被視為已存在），建議盡量設定 `ReadingField`。

## 部署（Docker）

1. 準備 `.env`（由 `.env.example` 複製，填入正式 bot token、白名單 user id、sync server 帳密與 `ANKI_SYNC_ENDPOINT`；留空時 bot 拒絕啟動）。設定 `TZ`（例如 `Asia/Taipei`，預設 UTC），`/stats` 的「今日」才會依你的時區換日。
2. 建立資料目錄並交給容器內使用者（uid / gid 1000）：`mkdir -p data && sudo chown 1000:1000 data`。主機使用者 uid 不是 1000 時也以此為準，或改用 build arg `APP_UID` / `APP_GID` 重建 image。
3. 依「初始化」一節執行 bootstrap，再 `docker compose up -d --build`。之後可選擇執行 `docker compose run --rm bot python scripts/build_tatoeba.py` 建立例句資料庫。
4. 查看狀態：`docker compose ps`（含 healthcheck 結果）、`docker compose logs -f bot`。

準備好 `.env` 後，步驟 2–3 也可以改用 `scripts/deploy.sh`：檢查 `.env`、準備 `data/`，`data/collection.anki2` 不存在時執行 bootstrap（先 dry run，確認後才套用），啟動 bot，`data/tatoeba.db` 不存在時建立例句資料庫。`git pull` 後可重複執行來更新。若出現 `Permission denied`，先執行 `chmod +x scripts/deploy.sh`（或改用 `bash scripts/deploy.sh`）。

注意事項：

- **同一份 `data/` 只能有一個 bot 容器**；啟動時會對 `/data/.lock` 取排他鎖，取不到就退出。bootstrap 也要在 bot 停止時執行。
- **Healthcheck**：bot 每分鐘呼叫一次 Telegram `getMe`，成功才更新 `/tmp/ankibot-heartbeat`；`python -m ankibot.healthcheck` 檢查該檔在 3 分鐘內更新過。它只保證 event loop 仍在運作且能連上 Telegram API，**不保證** polling 迴圈仍在接收更新，也不代表同步正常。
- **優雅關閉**：`docker compose stop` 送出 SIGTERM（`stop_grace_period: 60s`）。bot 依序停止接收更新 → 取消仍在產生語音的 Save（下次啟動顯示「⚠️ 中斷，請重新按 Save」）→ 有待 sync 的變更時做最後一次 sync（上限 `SHUTDOWN_SYNC_TIMEOUT_SECONDS`，預設 45 秒）→ 關閉 collection 並寫入預覽狀態。逾時，或沒有待上傳的變更但仍有 sync 在跑（例如首次媒體下載）時，不等待、不關閉 collection，寫入預覽狀態後直接結束（exit code 1）；已提交的資料由 SQLite WAL 保留，未完成的 sync 下次啟動重做。
- **備份**：`data/collection.anki2` 是重要資料。備份時先 `docker compose stop`，再複製整個 `data/`（含 `collection.anki2-wal` 若存在）；或依賴其他裝置（桌面版）的 Anki 內建備份。
- **磁碟空間**：媒體同步會把整個 collection 的**所有**媒體下載到 `data/collection.media/`（不只 bot 產生的音訊）。部署前先在桌面版確認媒體總大小並預留空間；首次媒體同步可能耗時很久，期間的 Save 會排隊、不會遺失。
- **Undo 會直接刪除 note**：即使該卡已同步到其他裝置並被複習過，複習紀錄也會一併消失，沒有確認步驟。
- **full sync 的已知限制**：從桌面版做 schema 變更到 bot 偵測到 full sync 要求之間寫入的 note，會在 `bootstrap --force` 的 full download 時遺失（`--force` 會先列出這些變更）。偵測到之後 bot 暫停所有寫入。

## 可選功能

- **語音（edge-tts，預設關閉）**：設定 `TTS_PROVIDER=edge` 開啟。Save 時以 `TTS_VOICE` 唸 `Reading`，檔案存在 `/data/tts/`（檔名為 `(voice, text)` 的 hash），再匯入 Anki 媒體。edge-tts 是非官方端點，失敗時卡片照常建立，`Audio` 留空並提示「語音產生失敗」。
- **例句（Tatoeba）**：執行 `make build-tatoeba`（或 `docker compose run --rm bot python scripts/build_tatoeba.py`）下載日文句子並建立 `/data/tatoeba.db`；檔案不存在時自動停用。索引存的是 fugashi / unidic-lite 的斷詞結果，**升級這兩個套件後要重新執行**。資料來源：[Tatoeba](https://tatoeba.org)，授權 [CC BY 2.0 FR](https://creativecommons.org/licenses/by/2.0/fr/)。
- **翻譯（DeepL 或 Gemini，二選一）**：設定 `DEEPL_API_KEY` 或 `GEMINI_API_KEY` 後，釋義、原句、例句會翻成目標語言（預設英文）；兩者都設定時啟動失敗。成功結果快取在 `/data/translations.db`（兩者分開存）。未設定時只顯示英文、完全不發請求；失敗時預覽只顯示英文並加一行「翻譯失敗」。
  - DeepL（**未實測**，只有單元測試；沒有用真實 API key 驗證過）：`:fx` 結尾的 Free key 自動使用 Free 端點。
  - Gemini：預設模型 `gemini-3.5-flash-lite`（`GEMINI_MODEL` 可改），免費額度用完時回 HTTP 429，預覽改為只顯示英文。每個新字約 2 個請求、不重試；實測一次傳約 60 個新字的長句會超過每分鐘上限，大部分預覽翻譯失敗（約 1 分鐘後恢復，重傳時已成功的翻譯走快取）。免費方案的輸入可能被 Google 用於改善模型。
- **翻譯目標語言**：
  - `TRANSLATION_TARGET`：DeepL 的 `target_lang` 代碼，預設 `EN-US`，例如 `ZH-HANT`、`ZH-HANS`、`KO`。DeepL 不接受單獨的 `EN`，要用 `EN-US` 或 `EN-GB`。
  - `TRANSLATION_TARGET_NAME`：Gemini prompt 裡的語言名稱（例如 `Korean`）。留空時 `ZH-HANT` 用「Traditional Chinese as used in Taiwan」，其他語言直接用代碼。
  - 快取依目標語言分開存，換語言不必清 `translations.db`。
  - 卡片模板 `back.html` 的 `lang="en"` 不會跟著變，要自行改成對應語言（繁體中文為 `zh-TW`）。
- **目標是英文時**（`EN`、`EN-US`、`EN-GB`，即預設）：JMdict 的釋義本來就是英文，**不會**為釋義呼叫翻譯 API。但原句與例句是日文，有設定 API key 時**仍會**呼叫 API 翻成英文：

  | 輸入 | 是否呼叫翻譯 API |
  | --- | --- |
  | 單字，沒有例句 | 不呼叫 |
  | 單字，有 Tatoeba 例句 | 呼叫（翻例句） |
  | 句子 | 呼叫（翻原句，有例句時也翻例句） |

  完全不想呼叫 API 時，不要設定 `DEEPL_API_KEY` / `GEMINI_API_KEY` 即可（原句與例句只顯示日文）。

## 介面語言與文字

bot 的所有訊息（狀態、按鈕、指令回覆、通知）都來自 `src/ankibot/locales/<語言>.toml`。

- `BOT_LANGUAGE`：內建語言，預設 `en`，目前有 `en`、`zh-Hant`。
- `BOT_MESSAGES_FILE`：可選，指向自訂的 TOML 檔（例如 `/data/messages.toml`），只需寫要改的鍵，其餘沿用 `BOT_LANGUAGE`。不必重 build image。
- 新增語言：複製 `en.toml` 為 `<代碼>.toml`（例如 `ja.toml`）並翻譯所有值，重 build 後設定 `BOT_LANGUAGE=<代碼>`。
- 格式規則：值是純文字（會自動 escape，不能用 HTML）；`{words}`、`{time}` 等 placeholder 必須與 `en.toml` 相同，字面上的大括號寫成 `{{`、`}}`。缺鍵、多出未知鍵或 placeholder 不符時，啟動直接報錯。
- 卡片模板（`templates/jp-bot-vocab/`）與 bootstrap 的 CLI 輸出不在此範圍。

## 授權

[AGPL-3.0-or-later](LICENSE)，與本專案依賴的 [Anki](https://github.com/ankitects/anki) 函式庫相同。

執行時使用的資料沿用各自的授權：

- [JMdict](https://www.edrdg.org/jmdict/j_jmdict.html)（透過 jamdict-data）：屬於 [Electronic Dictionary Research and Development Group](https://www.edrdg.org/)，依 [CC BY-SA 4.0](https://www.edrdg.org/edrdg/licence.html) 使用。
- [Tatoeba](https://tatoeba.org/) 例句（由 `scripts/build_tatoeba.py` 下載）：[CC BY 2.0 FR](https://creativecommons.org/licenses/by/2.0/fr/)。

## ☕ Support Me

<a href="https://www.buymeacoffee.com/hungliang" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
