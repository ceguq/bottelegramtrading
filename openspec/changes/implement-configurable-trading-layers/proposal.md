# OpenSpec Proposal: implement-configurable-trading-layers

## Summary
Add a configurable, user-managed concept of **trading layers** to the **localhost control panel UI**. A layer is a separately editable configuration block that the bot can later use to place orders according to user-defined settings.

This OpenSpec phase is **documentation-only**. It defines the UI/config design, validation responsibilities, and explicitly separates future phases for:
- order placement using layers
- DB tracking compatibility
- BE monitor compatibility

## Goals (UI + config only)
1. Add a new **“Layer Settings”** section in the localhost control panel.
2. Provide an **“+ Tambah Layer”** button to add layers.
3. Render each layer as an editable **card/detail section**.
4. Allow users to **manually configure** per-layer fields.
5. Allow users to **enable/disable** each layer.
6. Allow users to **remove** a layer.
7. Persist layer settings into `bot_config.json` under a new top-level `layers` field.
8. Ensure backward compatibility: if `layers` is missing, the system still loads safely using existing legacy fields.

## Non-Goals (must not be implemented in this phase)
- Do **not** change bot runtime logic.
- Do **not** modify the bot process orchestration (Start/Stop behavior must remain unchanged).
- Do **not** implement order placement from layers.
- Do **not** implement BE monitoring changes.
- Do **not** implement DB schema changes.
- Do **not** implement DB tracking for layered orders.

## Safety Constraints
- This is **OpenSpec/documentation only**.
- Do not edit the following runtime files in this phase:
  - `local_control_panel.py`
  - `mt5_executor.py`
  - `telegram_listener.py`
  - `be_monitor.py`
  - `db.py`
  - `bot_config.json`
- Do not run scripts (bot/MT5/Telegram/DB/order scripts).
- Do not commit or push.
- No credentials/secrets may appear in the UI.

## Backward Compatibility Requirements
1. Existing config fields must remain supported exactly as-is:
   - `lot`
   - `pip`
   - `tp1_pips`
   - `tp2_pips`
   - `sl_buffer`
   - `emergency_sl_pips`
   - `monitor_interval`
   - `telegram_test_mode`
   - `source_chat_id`
2. New config must be backward compatible:
   - If `layers` is absent, the system must fall back to the legacy single-settings fields without failing.
   - If `layers` is present, legacy fields must still be preserved (so older codepaths can still read them safely, if needed during transitional releases).
3. UI/config validation must never remove existing supported legacy fields.

## Suggested Config Shape (target for future implementation)
```json
{
  "layers": [
    {
      "name": "L1",
      "enabled": true,
      "lot": 0.01,
      "tp_enabled": true,
      "tp_pips": 50,
      "be_enabled": false,
      "be_trigger_pips": 50,
      "be_offset_pips": 0,
      "comment": "TG-L1"
    },
    {
      "name": "L2",
      "enabled": true,
      "lot": 0.01,
      "tp_enabled": true,
      "tp_pips": 100,
      "be_enabled": true,
      "be_trigger_pips": 50,
      "be_offset_pips": 0,
      "comment": "TG-L2"
    }
  ]
}
```

## Explicit Phase Separation (Required)
This OpenSpec must clearly separate responsibilities into the following future phases:
1. **UI/config phase**: add/remove/edit layers and save into config.
2. **Config validation phase**: validate layer schema/types and produce sanitized config.
3. **Order placement phase (later)**: bot uses layers to place orders.
4. **DB compatibility phase (later)**: DB tracking supports layered behavior.
5. **BE monitor phase (later)**: break-even monitor supports layered behavior.

## Acceptance Criteria for This OpenSpec Phase
- Only OpenSpec files are created.
- No bot runtime logic is changed.
- No runtime scripts are run.
- No commit or push.
- No deletion of files.

