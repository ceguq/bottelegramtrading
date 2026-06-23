# PROJECT RECAP - BOT_TRADING_TELEGRAM_V1

## 1. Ringkasan

Project **BOT_TRADING_TELEGRAM_V1** adalah bot **Telegram Signal → MT5 (MetaTrader 5)** untuk eksekusi trading **XAUUSD** (umumnya symbol broker: `XAUUSD.vxc`).

- Sinyal masuk dari **Telegram chat/channel** (`source_chat_id`).
- Bot melakukan **parsing** sinyal (direction, range entry, SL).
- Bot membuat **pending order** di MT5 (TP berlapis) dan menyimpan state order ke **SQLite** agar bisa dipantau lintas proses.
- Fitur utama: **Breakeven monitor (BE)** yang memindahkan SL untuk TP2 berdasarkan kondisi TP1.
- Terdapat **control panel lokal** untuk melihat status proses dan mengirim perintah **start/stop** bot:
  - http://127.0.0.1:8765

## 2. Struktur Project

| Path | Fungsi singkat | Catatan penting |
|---|---|---|
| `run_bot.py` | Runner utama; start child process `telegram_listener.py` dan `be_monitor.py`, guard single-instance, baca `run_bot.stop` untuk safe stop | Harus dijalankan pakai interpreter `..\.venv\Scripts\python.exe` agar child process ikut venv yang sama. Support single-stack lock: `run_bot.stack.lock`. |
| `local_control_panel.py` | Web control panel localhost via HTTPServer; tampilkan status proses; form edit config; endpoint start/stop | Bind hanya `127.0.0.1`. Safety: tidak mengekspos credential. Stop menggunakan file `run_bot.stop` (bukan kill PID langsung). Restart disabled. |
| `telegram_listener.py` | Listener Telegram; parsing sinyal; memanggil `mt5_executor.py` untuk validasi & placement order; simpan order ke DB | Defense-in-depth: refuse jalan jika tidak memakai venv python project. Pada test mode (config `telegram_test_mode=true`), hanya melakukan check order (tanpa kirim order). |
| `be_monitor.py` | Monitor BE; memantau order/posisi aktif dari DB; mengaplikasikan logic TP1→BE(TP2) dan update DB | Defense-in-depth: refuse jalan jika tidak memakai venv python project. Menjalankan loop monitor dengan lock MT5 per-cycle. |
| `mt5_executor.py` | Modul koneksi MT5 & eksekusi order (pending), plus logic SL update/cancel | Semua akses MT5 dibungkus `mt5_process_lock()` agar tidak bentrok antar proses. Eksekusi pending/SL update selalu memakai `mt5.order_check()` dulu. |
| `bot_config.json` | Konfigurasi safe operasional | Diisi field: `lot`, `pip`, `tp1_pips`, `tp2_pips`, `sl_buffer`, `emergency_sl_pips`, `monitor_interval`, `telegram_test_mode`, `source_chat_id`. |
| `bot_settings.py` | Loader & validasi konfigurasi dari `bot_config.json` | Parsing strict (tanpa default diam-diam). Menghasilkan data bertipe aman untuk dipakai file lain. |
| `db.py` | SQLite helper untuk `active_orders.db` | Menyimpan tracking order aktif. Menyediakan query pending terbaru & update flag `be_moved` dan status cancel. |
| `mt5_lock.py` | Cross-process lock untuk serialized access ke MT5 | Mengunci dengan file lock `active_orders.mt5.lock` menggunakan mekanisme OS (Windows msvcrt flock analog). |
| `.gitignore` | Aturan file yang tidak boleh commit | Meng-ignore `.venv/`, `*.session*`, database runtime, lock files, dll. |

## 3. Fungsi File Utama

### `run_bot.py`

**Peran**: runner utama.

- **runner utama**: menjalankan dua child process:
  - `telegram_listener.py`
  - `be_monitor.py`
- **guard venv**: memverifikasi `sys.executable` sama dengan:
  - `..\.venv\Scripts\python.exe`
  Jika tidak sesuai, script exit dengan kode `1`.
- **single stack / process guard**:
  - memakai lock file `run_bot.stack.lock` melalui class `_SingleInstanceGuard`.
  - jika stack lain masih running, `run_bot.py` menolak start.
- **output piping**:
  - stdout/stderr child process dipompa ke console dengan prefiks:
    - `[TELEGRAM]` dan `[BE]`.
- **support stop request file**: ketika file `run_bot.stop` ada,
  - `run_bot.py` akan:
    1. `_stop_all(processes)` → request stop child (graceful), lalu force jika perlu.
    2. menghapus `run_bot.stop`.
    3. exit bersih.

> Catatan implementasi: stop child memakai `CTRL_BREAK_EVENT` pada Windows (atau SIGINT pada POSIX), lalu fallback terminate/kill jika child tidak berhenti dalam timeout.

### `local_control_panel.py`

**Peran**: control panel lokal berbasis HTTP.

- **panel localhost**:
  - `HOST = "127.0.0.1"`
  - `PORT = 8765`
- **routes**:
  - `GET /` : halaman HTML (config safe fields + tombol start/stop + status).
  - `GET /api/status` : JSON status (hanya info proses; tanpa secrets).
  - `POST /config` : form submit config (field aman saja) → menyimpan ke `bot_config.json` dengan backup.
  - `POST /bot/start` dan `POST /bot/stop` (juga support path `/api/bot/start`, `/api/bot/stop`).
  - `POST /bot/restart` : **disabled**.
- **lampu status**:
  - hijau: BOT RUNNING (stack lengkap terdeteksi)
  - merah: BOT STOPPED
  - kuning: BOT PARTIAL / CHECK NEEDED (beberapa process ada, tidak lengkap)
- **proses & status**:
  - status dihitung dari inspeksi proses Windows (script names: `run_bot.py`, `telegram_listener.py`, `be_monitor.py`).
  - UI menampilkan hint status sesuai agregasi.
- **Start Bot**:
  - menjalankan `run_bot.py` dengan venv python (`.venv\Scripts\python.exe`) secara non-blocking.
  - stdout/stderr diarahkan ke `run_bot.panel.log`.
  - menghapus file `run_bot.stop` jika ada sebelum start.
- **Stop Bot** (safe):
  - tidak kill PID langsung.
  - membuat file `run_bot.stop`.
  - `run_bot.py` membaca file itu untuk menghentikan child process miliknya sendiri dan exit.

### `telegram_listener.py`

**Peran**: listener Telegram → parsing sinyal → kirim order ke MT5 → simpan state ke DB.

- **koneksi Telegram**: menggunakan Telethon (`TelegramClient`) dengan session lokal `xauusd_signal_session`.
- **mendengar sinyal dari `source_chat_id`**:
  - handler: `@client.on(events.NewMessage(chats=SOURCE_CHAT_ID))`
- **parsing sinyal**:
  - `parse_signal(text)` mengekstrak:
    - direction (`buy`/`sell`, mendukung alias `sel`)
    - range entry (dua token: `price_a-price_b`)
    - SL (format `sl ...`)
  - mendukung **expansion** bila entry/SL ditulis “short integer” (aturan `_expand_short_price`).
  - membuat plan order melalui `_build_order_plan()`:
    - memilih entry yang benar untuk buy/sell
    - mengubah SL menjadi final:
      - bila SL ada: buffered (`sl_buffer` via `PIP`)
      - bila SL tidak ada: emergency SL (`emergency_sl_pips`)
    - TP1/TP2 dihitung dari pip config.
- **follow-up SL update**:
  - `parse_sl_update(text)` mendeteksi pesan SL-only (tanpa membuat signal baru).
  - kemudian mencari order group latest dari DB (`get_latest_active_order()`)
  - memanggil `update_sl_for_order_group()` di MT5 executor.
- **panggil `mt5_executor.py`**:
  - real mode: `place_orders(...)` lalu DB insert `insert_order(...)` berdasarkan ticket.
  - test mode (`telegram_test_mode=true`): hanya `check_orders(...)` (tidak place order).
- **mencatat order aktif ke DB**:
  - insert mengisi `ticket_tp1`, `ticket_tp2`, `ticket_tp3` (jika placement berhasil 3 order) dan entry values.

### `be_monitor.py`

**Peran**: monitor breakeven untuk TP2 (BE SLTP).

- **monitor order/posisi aktif**:
  - mengambil DB rows aktif via `get_pending_orders()`.
  - mengambil kondisi MT5:
    - pending orders (filtered by symbol + magic)
    - positions (filtered by symbol + magic)
- **break even / watcher**:
  - BE kandidat hanya untuk order group yang belum `be_moved`.
  - logika BE:
    1. **buktikan TP1 sudah closed** dengan profit positif dan reason terkait TP (defensive dengan `getattr`).
    2. cari posisi aktif untuk TP2/TP3 berdasarkan comment dan ticket/history snapshot.
    3. tentukan BE SL:
       - untuk BE TP2: biasanya mengunci SL ke **TP2 posisi open price**.
    4. lakukan safety check:
       - `mt5.order_check(request)` untuk SLTP
       - pastikan BE SL tidak melewati batas bid/ask yang benar
       - pastikan jarak BE SL tidak melanggar `trade_stops_level`.
    5. jika lolos: kirim `mt5.order_send(request)`.
    6. update DB: `mark_be_moved(order_id, tp2_position_ticket=..., tp3_position_ticket=...)`.
- **anti-repeat**:
  - hanya memproses DB rows yang memenuhi kriteria (be_moved=0 dan pending_cancelled=0).
- **MT5 concurrency control**:
  - setiap cycle monitor memakai `with mt5_process_lock(timeout=30):`.

### `mt5_executor.py`

**Peran**: semua interaksi MT5.

- **koneksi MT5** (`connect()`):
  - inisialisasi MT5 menggunakan path terminal (`MT5_PATH`).
  - login dengan `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`.
  - validasi `trade_allowed`.
  - ada retry logic di case IPC timeout.
- **validasi order** (`check_orders(...)`):
  - memeriksa:
    - symbol/tick
    - broker constraints: digits, trade_stops_level, volume min/max/step
    - orientasi SL/TP sesuai direction
    - jarak minimum terhadap market & antar level
  - membentuk request pending untuk TG-TP1, TG-TP2, dan TP3(no-TP) lalu `mt5.order_check()` per request.
- **place pending order** (`place_orders(...)`):
  - membuat 3 pending order sesuai komponen:
    - `TG-TP1`
    - `TG-TP2`
    - `TG-NO-TP`
  - tiap order memakai `order_check` dulu; jika sukses `order_send` dan ambil ticket.
  - return tickets dalam urutan target.
- **TP/SL logic**:
  - pips → entry/TP/SL di `_build_levels()`.
  - symbol disesuaikan dengan broker `SYMBOL`.
- **konfigurasi yang dipakai**:
  - memuat safe values dari `bot_settings.py`:
    - `LOT`, `PIP`, `TP1_PIPS`, `TP2_PIPS`, `SL_BUFFER`.
  - identifier:
    - `MAGIC`, `SLIPPAGE`.

## 4. Alur Bot

1. User menjalankan:
   - `..\venv\Scripts\python.exe .\run_bot.py`
2. `run_bot.py` memulai child processes:
   - `telegram_listener.py`
   - `be_monitor.py`
3. `telegram_listener.py`:
   - connect Telegram
   - listen pesan baru dari `source_chat_id`
   - parse sinyal (direction, entry range, SL)
   - hitung TP/SL berdasarkan config pips
   - panggil `mt5_executor.py` untuk `place_orders(...)`
   - simpan order state ke SQLite via `db.insert_order(...)`
4. `be_monitor.py`:
   - loop tiap `monitor_interval`
   - baca DB pending order group (`get_pending_orders()`)
   - cek kondisi MT5 (pending & positions) untuk magic+symbol
   - jika TP1 closed by TP dengan profit positif:
     - hitung BE SL
     - lakukan `order_check` lalu `order_send` untuk SLTP pada TP2 (dan preservasi TP untuk TP3 sesuai kebutuhan)
     - update DB `mark_be_moved(...)`
5. `local_control_panel.py` (web UI) berjalan terpisah sebagai proses HTTP:
   - user bisa lihat status stack dan mengirim stop request.

## 5. Localhost Control Panel

Control panel berjalan di:
- http://127.0.0.1:8765

Fitur utama (berdasarkan implementasi):
- `GET /` : halaman HTML
- status lamp (hijau/kuning/merah) berdasarkan inspeksi proses
- `GET /api/status` : JSON status stack
- form edit konfigurasi safe fields → menyimpan ke `bot_config.json` (backup otomatis)
- tombol Start Bot:
  - memanggil start handler yang menjalankan `run_bot.py` non-blocking
- tombol Stop Bot:
  - menulis `run_bot.stop` (safe stop) dan menunggu polling status
- Restart:
  - disabled (UI dan endpoint sama-sama menolak restart)

## 6. Safe Stop Flow

Flow stop dirancang agar **panel tidak membunuh PID langsung**.

1. `local_control_panel.py` menangani request Stop.
2. Panel membuat file `run_bot.stop`.
3. `run_bot.py` (loop utama) mendeteksi keberadaan `run_bot.stop`.
4. `run_bot.py` menghentikan child process-nya sendiri (`telegram_listener.py` dan `be_monitor.py`) melalui `_stop_all(...)`.
5. `run_bot.py` menghapus `run_bot.stop`.
6. `run_bot.py` exit bersih.
7. Panel tetap hidup (HTTP server tetap serve).

## 7. Config

### `bot_config.json`

Field yang dipakai (safe keys):
- `lot`
- `pip`
- `tp1_pips`
- `tp2_pips`
- `sl_buffer`
- `emergency_sl_pips`
- `monitor_interval`
- `telegram_test_mode`
- `source_chat_id`

Panel (`local_control_panel.py`) hanya mengizinkan edit field-field aman di daftar SAFE_KEYS.

### Validasi di `bot_settings.py`

- parsing strict (tiap field harus ada dan bertipe benar)
- rule ringkas:
  - `lot > 0`
  - `pip > 0`
  - `tp1_pips > 0`
  - `tp2_pips > 0` dan `tp2_pips >= tp1_pips`
  - `sl_buffer >= 0`
  - `emergency_sl_pips > 0`
  - `monitor_interval >= 1`
  - `telegram_test_mode` harus boolean
  - `source_chat_id` harus integer

## 8. Command Penting

### Jalankan panel

```bat
..\venv\Scripts\python.exe .\local_control_panel.py
```

### Buka panel

- http://127.0.0.1:8765

### Cek status

```powershell
Invoke-WebRequest http://127.0.0.1:8765/api/status -UseBasicParsing
```

### Start Bot

```powershell
Invoke-WebRequest -Method POST http://127.0.0.1:8765/bot/start -UseBasicParsing
```

### Stop Bot

```powershell
Invoke-WebRequest -Method POST http://127.0.0.1:8765/bot/stop -UseBasicParsing
```

### Cek proses (PowerShell)

```powershell
Get-CimInstance Win32_Process |
Where-Object { $*.Name -match 'python' -and $*.CommandLine -match 'BOT_TRADING_TELEGRAM|run_bot.py|telegram_listener.py|be_monitor.py|local_control_panel.py' } |
Select-Object ProcessId,ParentProcessId,CommandLine |
Format-List
```

## 9. Safety Notes

- Jangan klik **Stop** saat ada entry aktif kecuali sadar bahwa BE monitor/watcher akan berhenti mengikuti safe stop flow.
- Entry MT5 **tidak otomatis terhapus** ketika bot stop; hanya proses yang dihentikan.
- Jangan menjalankan `run_bot.py` memakai global python; gunakan venv agar tidak terjadi duplicate bot stack.
- Panel **tidak expose credential** Telegram/MT5. Perubahan config hanya mencakup safe keys.
- MT5 akses di-serialize antar proses menggunakan lock (`mt5_lock.py`).

## 10. Git / File Runtime

File yang tidak boleh di-commit (runtime/secrets/state):

- `.venv/`
- `**/__pycache__/**`, `*.pyc`
- `*.log`
- `*.session`, `*.session-journal`
- `active_orders.db`
- `active_orders*.db`
- `*.db-wal`, `*.db-shm`
- `bot_config.backup.json`
- `run_bot.stop`
- `run_bot.panel.log`
- `local_control_panel.error.log`
- `active_orders.mt5.lock`
- `run_bot.stack.lock`

## 11. Status Fitur Terakhir

Berdasarkan implementasi file yang dibaca:
- Control panel:
  - `GET /api/status` tersedia dan tidak memuat secrets.
  - Start/Stop tersedia.
  - Restart **disabled**.
- Safe stop flow:
  - Stop menggunakan `run_bot.stop` (bukan kill PID langsung dari panel).
- Bot lifecycle:
  - `run_bot.py` memiliki single-instance guard via `run_bot.stack.lock`.
- BE logic:
  - BE berjalan sebagai child di stack bersama `telegram_listener.py` namun dipisah proses; memantau DB + MT5 kondisi dan hanya melakukan SLTP setelah `order_check` lolos.

---

## File yang dibaca (read-only)

- `run_bot.py`
- `local_control_panel.py`
- `telegram_listener.py`
- `be_monitor.py`
- `mt5_executor.py`
- `bot_config.json`
- `bot_settings.py`
- `db.py`
- `mt5_lock.py`
- `.gitignore`
- `PROJECT_DOCUMENTATION.md`

