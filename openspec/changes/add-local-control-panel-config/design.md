# OpenSpec Design: Local Panel Config (Central JSON + Safe Parameter Wiring)

## 1) Why centralize config first
- **User experience**: editing trading parameters should not require opening Python source files.
- **Operational correctness**: a single config file ensures that values used across modules (`telegram_listener`, `mt5_executor`, `be_monitor`) are consistent.
- **Validation & auditability**: config loader can enforce a schema and log load errors, preventing silent misconfiguration.

## 2) What settings are safe to expose
Safe operational settings (configurable in `bot_config.json`, non-secret):
- `LOT` (positive numeric)
- `TP1_PIPS` / `TP2_PIPS` (positive numeric)
- `SL_BUFFER` (numeric; can be 0)
- `EMERGENCY_SL_PIPS` (positive numeric)
- `MONITOR_INTERVAL` (seconds; positive numeric)
- `NEAR_ENTRY_MAX_PIPS` (non-negative numeric)
- `MISS_ENTRY_RUNAWAY_CANCEL_PIPS` (positive numeric)

Also explicitly excluded from config:
- MT5 secrets: MT5_PASSWORD
- Telegram secrets: API_ID / API_HASH / PHONE
- Any identifiers that could be considered security-sensitive (depending on future policy)

## 3) What settings should NOT be exposed (Phase 1–3)
Never load secrets from `bot_config.json`:
- MT5 password
- Telegram API credentials (API_ID/API_HASH/PHONE)

Additionally, Phase 1 scope must not expand to include:
- `SOURCE_CHAT_ID` (though the user requested it in the task, this design locks it to code-only for Phase 1 safety in this audit-only proposal unless explicitly updated later)

> Note: This design doc mirrors the “never secrets from config” constraint and can later be adjusted if you choose to allow non-secret identifiers like `SOURCE_CHAT_ID`.

## 4) Localhost-only panel safety
- Panel binds to `127.0.0.1` only.
- No NAT / no external binding.
- No public static file exposure beyond what is needed for local use.
- If the UI reads logs/DB, it should only show non-secret operational diagnostics.

## 5) Config loader design (`bot_settings.py` planned)
### Inputs
- `bot_config.json` from project root.

### Outputs
- Module-level constants (or getters) for safe runtime parameters.

### Behavior
- Load once at process startup.
- Provide safe defaults if config file missing.
- Validate types and bounds.
- On invalid config: log clear error including file path and key; fall back to defaults for those keys.
- On load errors (JSON parse errors, type mismatch): log errors and use defaults.

### Startup log requirement
At import/startup, log a structured summary including:
- `LOT`
- `TP1_PIPS`
- `TP2_PIPS`
- `SL_BUFFER`
- `EMERGENCY_SL_PIPS`
- `MONITOR_INTERVAL`
- `NEAR_ENTRY_MAX_PIPS`
- `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`

## 6) Runtime wiring design (Phase 2)
- Replace hardcoded values in the three target modules with values imported from `bot_settings.py`.
- Keep derived constants and comments unchanged.
- Ensure order comments remain exactly:
  - `TG-TP1`, `TG-TP2`, `TG-NO-TP`
- Ensure BE logic behavior and missed-entry cleanup behavior remain unchanged.

## 7) Validation rules (basic)
Suggested minimal validation (Phase 1):
- `LOT`: float > 0
- `TP1_PIPS`, `TP2_PIPS`, `EMERGENCY_SL_PIPS`, `MISS_ENTRY_RUNAWAY_CANCEL_PIPS`: int/float > 0
- `SL_BUFFER`: int/float >= 0 (or allow negative only if existing logic supports it; default is 10 in many bots but current code uses 0)
- `MONITOR_INTERVAL`: int/float > 0
- `NEAR_ENTRY_MAX_PIPS`: int/float >= 0

Validation strategy:
- If a key fails validation: use default value for that key.
- Never stop the bot due to config errors (unless config schema is totally unusable).

## 8) Live reload vs restart-required
- **Restart-required** for correctness.
- The panel may edit config file, but bot processes keep in-memory values until restarted.
- Phase 4 can enforce restart before critical updates take effect.

## 9) Duplicate stacks avoidance
Current `run_bot.py` already provides a stack-level guard.
Future Phase 3/4 design:
- The panel should call `run_bot.py` start/stop scripts only once.
- Phase 4 should extend safety so a user cannot start multiple stacks accidentally.

## 10) Preserve existing trading behavior during migration
- Do not change symbol, order types, magic number, slippage, comments, BE thresholds beyond those read from config.
- Keep DB schema and semantics identical.
- Keep MT5 and Telegram credentials code-only.

## 11) Risks & mitigations
1. **Mismatch across modules**: If `mt5_executor` and `telegram_listener` use different values, order calculations differ.
   - Mitigation: central loader is the single source of truth.
2. **Invalid config**: causes MT5 `order_check` failures.
   - Mitigation: validation + defaults.
3. **Operational mismatch**: if panel edits config but bot is not restarted.
   - Mitigation: restart-required warning + UI enforcement in Phase 4.

---

This is an audit-only design document. No code changes occur in this step.

