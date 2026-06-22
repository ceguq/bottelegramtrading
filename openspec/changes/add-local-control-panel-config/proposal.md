# OpenSpec Proposal: Local Panel Config (Phase 1–4 plan, no implementation in this step)

## Summary
Introduce a centralized, editable JSON configuration file to make safe trading parameters easier to modify without editing Python source code.

This proposal documents an implementation plan with phased rollout:
- **Phase 1**: Central config file + config loader, move *safe operational parameters* out of Python hardcoding.
- **Phase 2**: Update trading modules to read runtime values from the config loader.
- **Phase 3**: Add a **localhost-only** web panel (view/edit/validate safe config; start/stop/restart bot; read status).
- **Phase 4**: Operational safety improvements (single-instance enforcement across stacks; restart-required semantics; warnings if active DB/positions exist).

## Current State (Audit Findings)
Hardcoded parameters are currently defined directly in:
- `mt5_executor.py`
  - `LOT`, `TP1_PIPS`, `TP2_PIPS`, `SL_BUFFER`, and `PIP` (XAUUSD pip factor), plus `MONITOR_INTERVAL`.
  - MT5 credentials and MT5 terminal path are also hardcoded here.
- `telegram_listener.py`
  - Telegram credentials (`API_ID`, `API_HASH`, `PHONE`)
  - `SOURCE_CHAT_ID`
  - `TELEGRAM_TEST_MODE`
  - `LOT`, `TP1_PIPS`, `TP2_PIPS`, `SL_BUFFER`, `EMERGENCY_SL_PIPS`, `MONITOR_INTERVAL`
- `be_monitor.py`
  - `NEAR_ENTRY_MAX_PIPS`, `MISS_ENTRY_RUNAWAY_CANCEL_PIPS` (and related near-entry/runaway thresholds)
  - `MONITOR_INTERVAL` and additional BE logic thresholds.

Process orchestration:
- `run_bot.py` starts `telegram_listener.py` and `be_monitor.py` as child processes.
- `run_bot.py` already implements **single-instance guard** via a stack lock file (`run_bot.stack.lock`) and Windows PowerShell process scanning.

## Goals
1. Create `bot_config.json` as a centralized, human-editable source for safe trading parameters.
2. Ensure runtime parameters are loaded from the config layer (via a future helper module `bot_settings.py`).
3. Prevent secrets (MT5 password, Telegram credentials) from being loaded from config.
4. Add clear logging of loaded safe parameters at startup.

## Non-Goals / Explicit Constraints
- Do **not** move secrets into config (MT5_PASSWORD, Telegram API_ID/API_HASH/PHONE remain code-only or environment-based later).
- Do **not** change trading behavior (order comments, BE logic behavior, missed-entry cleanup behavior).
- Do **not** change `run_bot.py` process behavior in this step.

## Proposed Config File
`bot_config.json` (placed in project root alongside Python scripts)

Initial keys (Phase 1 scope only):
- `LOT`
- `TP1_PIPS`
- `TP2_PIPS`
- `SL_BUFFER`
- `EMERGENCY_SL_PIPS`
- `MONITOR_INTERVAL`
- `NEAR_ENTRY_MAX_PIPS`
- `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`

## Validation & Safety Approach (Phase 1)
A new helper module (`bot_settings.py`) will:
- Read `bot_config.json` from the same folder as the project files.
- Provide safe default values when the file is missing.
- Validate basic numeric types and bounds (e.g., LOT > 0, pips > 0, monitor interval > 0).
- Log clear load errors.
- Never load MT5 or Telegram secrets from config.

## Phases
### Phase 1: Central config file only (no runtime wiring yet)
- Add `bot_config.json`
- Add config loader helper (`bot_settings.py` in final implementation)
- Move only safe operational settings into config schema.
- Do **not** modify execution logic in trading modules yet.

### Phase 2: Runtime modules read config (no behavior change)
Update:
- `mt5_executor.py`
- `telegram_listener.py`
- `be_monitor.py`

Replace hardcoded safe parameters with values imported from the config helper.

### Phase 3: Localhost web panel
Provide a 127.0.0.1-only UI:
- View current config
- Edit safe config values
- Validate before saving
- View bot process status
- View DB/MT5 summaries (pending orders, positions, active DB rows)
- Start/stop/restart bot (optional controls)
- Show latest logs

### Phase 4: Operational safety
- Prevent duplicate stacks
- Require restart after changing startup-loaded values
- Show warnings if DB indicates pending active rows or MT5 has active positions
- Ensure UI does not expose MT5/Telegram secrets publicly

## Risks Identified (Draft)
1. **Runtime consistency**: If config changes while bot processes are running, modules may continue using old in-memory values until restart.
   - Mitigation: document restart-required semantics.
2. **Invalid config causing order rejection**: mis-typed values or out-of-range values can break order calculation or MT5 `order_check`.
   - Mitigation: strict validation + fallback defaults.
3. **Accidental secret exposure**: a config loader might be implemented to read secrets.
   - Mitigation: explicit schema exclusion for secrets.
4. **Concurrency when adding config panel**: config edits while BE monitor reads/writes DB.
   - Mitigation: config changes are treated as restart-required; panel should not attempt live mutation of BE thresholds.

## What must change in future implementation (Targets)
Phase 2 wiring targets:
- `mt5_executor.py`: `LOT`, `TP1_PIPS`, `TP2_PIPS`, `SL_BUFFER`, and derived calculations.
- `telegram_listener.py`: `TELEGRAM_TEST_MODE`, `SOURCE_CHAT_ID`, and signal parsing thresholds that use pips/buffers.
- `be_monitor.py`: `MONITOR_INTERVAL`, `NEAR_ENTRY_MAX_PIPS`, `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`.

## Restart Requirement
Any Phase 2 values loaded at process import/startup should require bot restart to take effect.

## Open Questions (for later phases)
- Whether the web panel will enforce restart by killing child processes (run_bot-managed) or only instruct user.
- Exact validation boundaries for pips/intervals.

---

This proposal is documentation-only for now; no runtime changes are implemented in this step.

