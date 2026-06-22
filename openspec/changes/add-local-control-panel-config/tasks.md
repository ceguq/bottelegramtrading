# OpenSpec Tasks: Local Panel Config (Audit-Only Docs)

> This tasks file breaks work into small checkboxes. Implementation is NOT performed in this audit-only step.

## Phase 1 — Central config file + config loader (no runtime wiring)
- [ ] Create `bot_config.json` in project root with keys:
  - [ ] `LOT`
  - [ ] `TP1_PIPS`
  - [ ] `TP2_PIPS`
  - [ ] `SL_BUFFER`
  - [ ] `EMERGENCY_SL_PIPS`
  - [ ] `MONITOR_INTERVAL`
  - [ ] `NEAR_ENTRY_MAX_PIPS`
  - [ ] `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`

- [ ] Create helper module `bot_settings.py`
  - [ ] Reads `bot_config.json` from the same folder as `bot_settings.py` / project root.
  - [ ] Provides safe default values when config file is missing.
  - [ ] Validates numeric values (basic type/bounds only).
  - [ ] Logs/prints clear config load errors (file path + key + reason).
  - [ ] Never reads/loads MT5 password or Telegram API credentials from config.
  - [ ] At startup, logs the loaded operational settings summary:
    - [ ] `LOT`
    - [ ] `TP1_PIPS`
    - [ ] `TP2_PIPS`
    - [ ] `SL_BUFFER`
    - [ ] `EMERGENCY_SL_PIPS`
    - [ ] `MONITOR_INTERVAL`
    - [ ] `NEAR_ENTRY_MAX_PIPS`
    - [ ] `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`

- [ ] Add Phase-1 validation checks (read-only)
  - [ ] Run compile only:
    - [ ] `python -m py_compile .\bot_settings.py`

## Phase 2 — Wire runtime modules to config (no behavior change)
- [ ] Update `mt5_executor.py` to read these values from `bot_settings.py` at import/startup:
  - [ ] `LOT`
  - [ ] `TP1_PIPS`
  - [ ] `TP2_PIPS`
  - [ ] `SL_BUFFER`
  - [ ] (derived pip math stays same) `PIP`
  - [ ] Ensure order comments remain exactly `TG-TP1`, `TG-TP2`, `TG-NO-TP`

- [ ] Update `telegram_listener.py` to read these values from `bot_settings.py`:
  - [ ] `TP1_PIPS`
  - [ ] `TP2_PIPS`
  - [ ] `SL_BUFFER`
  - [ ] `EMERGENCY_SL_PIPS`
  - [ ] `MONITOR_INTERVAL` if referenced here (audit first before wiring)
  - [ ] Do NOT move Telegram credentials into config.

- [ ] Update `be_monitor.py` to read these values from `bot_settings.py`:
  - [ ] `MONITOR_INTERVAL`
  - [ ] `NEAR_ENTRY_MAX_PIPS`
  - [ ] `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`

- [ ] Phase-2 behavior preservation checks (no execution)
  - [ ] Confirm BE logic behavior and missed-entry cleanup behavior are unchanged.
  - [ ] Confirm no MT5/Telegram credentials are sourced from config.

- [ ] Phase-2 validation (compile only)
  - [ ] `python -m py_compile .\mt5_executor.py`
  - [ ] `python -m py_compile .\telegram_listener.py`
  - [ ] `python -m py_compile .\be_monitor.py`

## Phase 3 — Localhost web panel (127.0.0.1 only)
- [ ] Add a minimal UI server (planned in later phase)
  - [ ] Bind only to `127.0.0.1`
  - [ ] Show current `bot_config.json`
  - [ ] Allow editing only safe operational keys
  - [ ] Validate config values before saving
  - [ ] Show bot process status (read-only)

- [ ] Add bot process control endpoints (planned)
  - [ ] Start/stop/restart by invoking existing `run_bot.py` behavior
  - [ ] Ensure no duplicate stacks

- [ ] Add read-only operational views (planned)
  - [ ] Active DB rows (query `active_orders.db`)
  - [ ] Pending MT5 orders summary (read-only; no mutation)
  - [ ] Active bot positions (read-only)
  - [ ] Latest logs

## Phase 4 — Operational safety
- [ ] Enforce restart-required semantics in UI
  - [ ] Warn user that config edits require bot restart

- [ ] Prevent duplicate stacks from UI
  - [ ] UI checks existing running stack state before start

- [ ] Add safety warnings for high-risk settings
  - [ ] If DB indicates active orders exist, warn before changing `LOT/TP/SL`
  - [ ] If MT5 indicates active positions exist, warn similarly

## Global validation checklist (for later implementation)
- [ ] Run compile-only for all listed files (no bot execution):
  - [ ] `python -m py_compile .\bot_settings.py`
  - [ ] `python -m py_compile .\mt5_executor.py`
  - [ ] `python -m py_compile .\telegram_listener.py`
  - [ ] `python -m py_compile .\be_monitor.py`
  - [ ] `python -m py_compile .\db.py`
  - [ ] `python -m py_compile .\run_bot.py`
  - [ ] `python -m py_compile .\mt5_lock.py`

- [ ] Do not run:
  - [ ] `run_bot.py`
  - [ ] `telegram_listener.py`
  - [ ] `be_monitor.py`
- [ ] Do not mutate DB or MT5 during validation

