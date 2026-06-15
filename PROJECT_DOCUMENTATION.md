# BOT_TRADING_TELEGRAM - Dokumentasi Lengkap

## 📋 Ringkasan Proyek

Proyek ini adalah **sistem auto-trading bot untuk XAUUSD (Gold)** yang terintegrasi dengan:
- **Telegram**: Menerima sinyal trading dari channel sinyal
- **MetaTrader 5 (MT5)**: Eksekusi order dan manajemen posisi secara otomatis
- **SQLite Database**: Penyimpanan state order yang persistent

Bot bekerja dengan strategi **2-TP (Two Take Profit)** dengan fitur **Breakeven (BE)** otomatis pada TP2.

---

## 📁 Struktur File

```
BOT_TRADING_TELEGRAM/
├── be_monitor.py              # Monitor breakeven untuk TP2
├── cari_chat_id.py            # Utility untuk menemukan Telegram chat ID
├── db.py                      # SQLite database handler
├── mt5_executor.py            # MT5 connection dan order execution
├── telegram_listener.py       # Telegram signal listener dan parser
├── test_mt5_connection.py     # Testing koneksi MT5
├── active_orders.db           # Database SQLite (state orders)
├── session_find_id.session    # Telethon session untuk cari_chat_id.py
├── xauusd_signal_session.session # Telethon session untuk telegram_listener.py
└── .venv/                     # Python virtual environment
```

---

## 📄 Detail File & Fungsi

### 1. **telegram_listener.py** - Main Telegram Signal Listener
**Tujuan**: Mendengarkan channel Telegram, parse sinyal, dan submit order ke MT5

**Fungsi Utama**:
- `parse_signal(text)` - Parse pesan Telegram untuk ekstrak direction, entry prices, dan SL
- `_expand_short_price(token, reference_price)` - Expand harga pendek menjadi harga lengkap
- `handle_signal(event)` - Event handler ketika pesan baru masuk ke channel sinyal
- `place_orders(...)` - Menempatkan 2 pending orders (TP1 dan TP2) ke MT5

**Konfigurasi Penting**:
```python
API_ID = 37673990
API_HASH = "a9a7c7a933318f577f7d16aeb05a63db"
PHONE = "+6281229995423"
SOURCE_CHAT_ID = -1003511779760  # Chat ID channel sinyal
TELEGRAM_TEST_MODE = False  # true=test mode, false=real execution
```

**Format Sinyal yang Diparsing**:
- Direction: `BUY` atau `SELL`
- Entry Range: `2080-2090` atau `80-90` (short format)
- Stoploss: `sl: 2070` atau `sl: 70`

**Contoh Sinyal**:
```
BUY 2080-2090
SL: 2070
```

---

### 2. **mt5_executor.py** - MT5 Connection & Order Execution
**Tujuan**: Koneksi ke MT5 dan eksekusi order dengan retry logic

**Fungsi Utama**:
- `connect()` - Inisialisasi MT5, login dengan retry untuk IPC timeout
- `place_orders(direction, entry_first, entry_second, sl)` - Place 2 pending orders
- `check_orders(order_1, order_2)` - Validate orders sebelum submit
- `get_current_reference_price()` - Fetch harga XAUUSD terkini

**Konfigurasi Penting**:
```python
MT5_LOGIN = 371836460
MT5_PASSWORD = "sw34LOG2311@"
MT5_SERVER = "ValetaxIntl-Live2"
MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

SYMBOL = "XAUUSD.vx"
LOT = 0.01  # Ukuran lot per order
TP1_PIPS = 50
TP2_PIPS = 100
SL_BUFFER = 10  # Buffer tambahan untuk SL
MAGIC = 20250611  # Magic number identifier
```

**Fitur Retry IPC**:
- Jika MT5 return IPC timeout error, akan retry hingga 3x dengan delay 5 detik
- Mencegah error transient

---

### 3. **be_monitor.py** - Breakeven Monitor untuk TP2
**Tujuan**: Monitor TP1 completion dan move TP2 SL ke breakeven

**Logika**:
1. Ambil pending orders dari DB (where `be_moved = 0`)
2. Cek TP1 closed by Take Profit dengan profit positif
3. Jika TP1 berhasil dengan profit > 0, move TP2's SL ke TP2's open price (breakeven)
4. Validasi dengan `mt5.order_check()` sebelum `mt5.order_send()`
5. Mark order sebagai `be_moved = 1` di DB

**Fitur Keamanan**:
- **Strict pairing**: Hanya process orders yang ada di DB
- **Profit verification**: TP1 harus close by TP dengan profit > 0
- **Order validation**: Check order sebelum submit
- **Anti-repeat**: Hanya process rows dengan `be_moved = 0`

**Konfigurasi**:
```python
MT5_LOGIN = 123456
MT5_PASSWORD = "password"
MT5_SERVER = "Valetax-Server"
MONITOR_INTERVAL = 5  # Cek setiap 5 detik

TP1_COMMENT = "TG-TP1"
TP2_COMMENT = "TG-TP2"
BE_COMMENT = "TG-TP2-BE"
```

---

### 4. **db.py** - SQLite Database Handler
**Tujuan**: Manage persistent order state antara `telegram_listener.py` dan `be_monitor.py`

**Database Schema** (`active_orders` table):
```sql
CREATE TABLE active_orders (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_tp1           INTEGER NOT NULL,       -- MT5 ticket untuk TP1 order
  ticket_tp2           INTEGER NOT NULL,       -- MT5 ticket untuk TP2 order
  direction            TEXT NOT NULL,          -- "BUY" atau "SELL"
  entry                REAL NOT NULL,          -- Entry price pertama
  entry_tp1            REAL,                   -- Entry price TP1 (fallback ke entry)
  entry_tp2            REAL,                   -- Entry price TP2 (fallback ke entry)
  be_moved             INTEGER DEFAULT 0,      -- Flag: TP2 SL sudah move ke BE?
  tp1_closed           INTEGER DEFAULT 0,      -- TP1 order closed?
  tp1_closed_by_tp     INTEGER DEFAULT 0,      -- TP1 closed by Take Profit?
  tp1_profit_positive  INTEGER DEFAULT 0,      -- TP1 profit > 0?
  tp2_position_ticket  INTEGER                 -- Position ticket TP2 setelah BE moved
)
```

**Fungsi Utama**:
- `init_db()` - Create table jika belum ada, migrate columns
- `insert_order(ticket_tp1, ticket_tp2, direction, entry_first, entry_second)` - Simpan order baru
- `get_pending_orders()` - Ambil orders dengan `be_moved = 0`
- `mark_be_moved(order_id, tp2_position_ticket)` - Mark order sudah di-move ke BE
- `mark_tp1_status(order_id, closed, closed_by_tp, profit_positive)` - Update TP1 status

---

### 5. **cari_chat_id.py** - Telegram Chat ID Finder
**Tujuan**: Utility script untuk menemukan Chat ID dari channel/group Telegram

**Cara Pakai**:
```bash
python cari_chat_id.py
```

**Output**: List semua dialog dengan ID-nya
```
        -1003511779760  |  Signal Channel Name
         123456789      |  Personal Chat
```

**Langkah Konfigurasi**:
1. Run script ini
2. Cari channel sinyal kamu di output
3. Copy angka negative ID (e.g., `-1003511779760`)
4. Paste ke `SOURCE_CHAT_ID` di `telegram_listener.py`

---

### 6. **test_mt5_connection.py** - MT5 Connection Tester
**Tujuan**: Validasi konfigurasi MT5 sebelum menjalankan bot

**Output Test**:
```
Configured MT5 path: C:\Program Files\MetaTrader 5\terminal64.exe
Company: Valetax
Connected: True
Trade allowed: True
Terminal path: C:\Program Files\MetaTrader 5
Account login: 371836460
Balance: 50000.00
Server: ValetaxIntl-Live2
MT5 connection OK — ready to trade
```

**Cara Pakai**:
```bash
python test_mt5_connection.py
```

**Return Code**:
- `0` - OK, siap trading
- `1` - Error (lihat output untuk detail)

---

### 7. **.session Files** - Telethon Session Storage
- `session_find_id.session` - Session file untuk `cari_chat_id.py`
- `xauusd_signal_session.session` - Session file untuk `telegram_listener.py`

Ini adalah file binary yang menyimpan autentikasi Telegram, sehingga tidak perlu re-login setiap kali.

---

### 8. **active_orders.db** - SQLite Database
Database file yang menyimpan state orders. Akan auto-create jika belum ada.

---

## 🔄 Workflow & Alur Kerja

### Workflow Lengkap:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. SETUP TELEGRAM                                           │
│    - Run: python cari_chat_id.py                           │
│    - Copy SOURCE_CHAT_ID ke telegram_listener.py           │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. TEST MT5 CONNECTION                                      │
│    - Run: python test_mt5_connection.py                    │
│    - Verify output: "MT5 connection OK — ready to trade"  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. START TELEGRAM LISTENER (Process 1)                      │
│    - Run: python telegram_listener.py                      │
│    - Mendengarkan signal dari Telegram channel             │
│    - Parse sinyal, place 2 pending orders di MT5           │
│    - Save order state ke active_orders.db                  │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. START BREAKEVEN MONITOR (Process 2 - Separate Terminal)  │
│    - Run: python be_monitor.py                             │
│    - Monitor TP1 completion setiap 5 detik                 │
│    - Jika TP1 close dengan profit > 0:                     │
│      * Move TP2's SL ke breakeven (TP2 open price)         │
│      * Update DB: mark_be_moved = 1                        │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. SIGNAL DARI CHANNEL TELEGRAM                            │
│    Contoh: "BUY 2080-2090 SL: 2070"                        │
│                                                             │
│    telegram_listener.py:                                   │
│    ├─ Parse: direction=BUY, entry1=2080, entry2=2090,     │
│    │          sl=2070                                      │
│    ├─ Calculate TP:                                        │
│    │  * TP1 = entry1 + 50 pips = 2085                      │
│    │  * TP2 = entry1 + 100 pips = 2090                     │
│    │  * SL = 2070 - 10 pips buffer = 2060                  │
│    ├─ Place 2 pending orders:                              │
│    │  * Order 1: BUY XAUUSD 0.01 lot @ 2080, TP=2085      │
│    │  * Order 2: BUY XAUUSD 0.01 lot @ 2090, TP=2090      │
│    └─ Save ke DB:                                          │
│       INSERT active_orders (ticket_tp1, ticket_tp2, ...)   │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 6. TP1 FILLED (Order 1 close by Take Profit)              │
│                                                             │
│    be_monitor.py:                                          │
│    ├─ Detect TP1 closed dengan profit > 0                 │
│    ├─ Validate TP2 masih open                             │
│    ├─ Calculate new SL (breakeven = TP2 entry price)      │
│    └─ Move TP2 SL ke breakeven                            │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│ 7. RESULT                                                   │
│    ✓ TP1: Profit +50 pips (DONE)                          │
│    ✓ TP2: Protected dengan breakeven SL (SAFE)            │
│    ✓ Order marked as be_moved = 1 di DB                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Cara Menjalankan Bot

### Prasyarat:
- Python 3.8+
- MetaTrader 5 (Valetax atau MetaQuotes)
- Telegram account dengan API credentials dari https://my.telegram.org

### Setup:

1. **Clone/Setup Folder**:
   ```bash
   cd e:\project\vscode\BOT_TRADING_TELEGRAM
   ```

2. **Create Virtual Environment**:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install telethon MetaTrader5
   ```

4. **Konfigurasi Telegram**:
   ```bash
   python cari_chat_id.py
   # Copy SOURCE_CHAT_ID ke telegram_listener.py
   ```

5. **Konfigurasi MT5**:
   - Update `MT5_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` di `mt5_executor.py`
   - Run: `python test_mt5_connection.py`

6. **Jalankan Bot** (2 terminal terpisah):

   **Terminal 1 - Telegram Listener**:
   ```bash
   .venv\Scripts\activate
   python telegram_listener.py
   ```

   **Terminal 2 - Breakeven Monitor**:
   ```bash
   .venv\Scripts\activate
   python be_monitor.py
   ```

---

## ⚙️ Konfigurasi Penting

### Global Settings (berlaku untuk semua file):

| Parameter | Lokasi | Nilai Default | Keterangan |
|-----------|--------|---------------|-----------|
| `SYMBOL` | mt5_executor.py | XAUUSD.vx | Pair trading |
| `LOT` | semua file | 0.01 | Ukuran lot |
| `TP1_PIPS` | semua file | 50 | Take profit 1 |
| `TP2_PIPS` | semua file | 100 | Take profit 2 |
| `SL_BUFFER` | semua file | 10 | Extra buffer untuk SL |
| `MAGIC` | semua file | 20250611 | Magic number identifier |
| `SLIPPAGE` | semua file | 20 | Max slippage points |
| `MONITOR_INTERVAL` | be_monitor.py | 5 | Cek breakeven setiap N detik |
| `TELEGRAM_TEST_MODE` | telegram_listener.py | False | Test mode tanpa order real |

### Telegram Settings:

| Parameter | File | Tujuan |
|-----------|------|--------|
| `API_ID` | telegram_listener.py | API ID dari my.telegram.org |
| `API_HASH` | telegram_listener.py | API hash dari my.telegram.org |
| `PHONE` | telegram_listener.py | Nomor Telegram (+62xxx) |
| `SOURCE_CHAT_ID` | telegram_listener.py | Chat ID channel sinyal |

### MT5 Settings:

| Parameter | File | Tujuan |
|-----------|------|--------|
| `MT5_LOGIN` | mt5_executor.py | Account login |
| `MT5_PASSWORD` | mt5_executor.py | Account password |
| `MT5_SERVER` | mt5_executor.py | Broker server name |
| `MT5_PATH` | mt5_executor.py | Path ke terminal64.exe |

---

## 🐛 Troubleshooting

### Error: "MT5 executable not found"
- **Solusi**: Update `MT5_PATH` di `mt5_executor.py` dengan path ke terminal64.exe yang benar
- Cara cari: Properties → Shortcut tab → Target field

### Error: "IPC Timeout"
- **Sebab**: MT5 tidak responsif
- **Solusi**: Tunggu beberapa detik, bot akan auto-retry 3x

### Error: "Trade allowed = False"
- **Solusi**: Enable "Algo Trading" di MT5
- Menu: Tools → Options → Expert Advisors → Allow Automated Trading

### Sinyal tidak ter-parse
- **Periksa format**: "BUY/SELL [range] SL: [value]"
- **Test**: Lihat log di console untuk debug message

### TP1/TP2 tidak terisi
- **Periksa**: Account punya cukup margin? Balance cukup?
- **Run test**: `python test_mt5_connection.py`

---

## 📊 Database Query Examples

### Lihat semua pending orders:
```sql
SELECT id, ticket_tp1, ticket_tp2, direction, entry, be_moved
FROM active_orders
WHERE be_moved = 0;
```

### Lihat completed trades:
```sql
SELECT id, ticket_tp1, ticket_tp2, tp1_closed_by_tp, tp1_profit_positive
FROM active_orders
WHERE be_moved = 1;
```

### Clean old data:
```sql
DELETE FROM active_orders
WHERE be_moved = 1 AND tp1_closed_by_tp = 1;
```

---

## 🔐 Security Notes

1. **Jangan commit credentials** ke git - gunakan .gitignore
2. **MT5_PASSWORD** harus di-mask saat deployment
3. **API credentials** dari Telegram harus dijaga keamanannya
4. Disable test mode (`TELEGRAM_TEST_MODE = False`) hanya saat siap live

---

## 📝 Log File Format

Bot menggunakan Python logging dengan format:
```
2025-06-15 10:30:45,123 [INFO] Telegram message received
2025-06-15 10:30:46,456 [INFO] Order saved to DB: tp1=1234 tp2=1235
2025-06-15 10:30:50,789 [INFO] Order id=1 marked as BE moved.
```

---

## 🎯 Summary

| Component | Peran | Input | Output |
|-----------|-------|-------|--------|
| `telegram_listener.py` | Signal parser & order placer | Telegram signal | MT5 orders + DB entry |
| `be_monitor.py` | Breakeven automation | DB orders + MT5 history | BE moved orders |
| `mt5_executor.py` | MT5 interface | Order params | Execution status |
| `db.py` | State management | Order data | Persistent storage |
| `cari_chat_id.py` | Setup utility | Telegram auth | Chat ID |
| `test_mt5_connection.py` | Connection tester | MT5 config | Health check |

**Status**: ✅ Bot siap untuk trading otomatis XAUUSD dengan fitur breakeven safety!
