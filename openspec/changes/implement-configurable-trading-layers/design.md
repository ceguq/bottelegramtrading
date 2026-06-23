# OpenSpec Design: implement-configurable-trading-layers

## 1) Scope
Design the future **localhost control panel UI** and the **config schema contract** for “configurable trading layers”.

This document is design-only. It does not require (and must not include) runtime code changes.

## 2) UX: Local Control Panel “Layer Settings” section

### Placement
- Add a new section titled **“Layer Settings”** in the localhost control panel page.
- It should sit logically near the existing “Config (Safe Fields)” section.

### Add Layer CTA
- Button label: **“+ Tambah Layer”**
- Behavior (future implementation):
  - Adds a new layer card with sensible defaults that keep config valid.
  - Does not change existing legacy fields.

### Per-Layer Card/Detail
Each layer is rendered as an editable card containing:
1. **Header row**
   - Layer identifier display (recommended field: `name`)
   - Optional small “enabled” toggle control (see below)
   - Remove action (see §2.5)

2. **Enable/Disable**
   - A user-controlled switch/checkbox to set `enabled: true/false`.
   - Disabled layers should be persisted but ignored by future order-placement logic.

3. **Manual configuration fields**
   - Inputs for the layer fields described in §4.
   - The UI must be careful to sanitize/validate values before saving (future validation phase).

4. **Comment / metadata**
   - Optional text field for `comment`.
   - Should be treated as non-secret user-provided metadata.

### Remove Layer
- Provide a remove button (e.g., trash icon) inside each card.
- Future implementation behavior:
  - Removes the layer from the `layers` array.
  - Must not delete legacy config fields.
  - If the user deletes all layers, `layers` may be removed entirely or left as an empty array—both must be safely supported.

### Save behavior
- Future implementation behavior:
  - Saving writes `layers` into `bot_config.json`.
  - Existing legacy safe fields continue to be saved exactly as before.
  - Existing safe Start/Stop behavior remains unchanged.

## 3) Security / Safety Rules (UI)
- No credentials/secrets shown.
- The UI must only display/edit non-secret parameters.
- Layer fields are configuration data only; do not display MT5 session/account credentials.

## 4) Layer configuration schema (target contract)

### Suggested fields (future implementation)
- `name` (string) — human readable identifier
- `enabled` (boolean)
- `lot` (number > 0)
- `tp_enabled` (boolean)
- `tp_pips` (int > 0)
- `be_enabled` (boolean)
- `be_trigger_pips` (int >= 0)
- `be_offset_pips` (int >= 0)
- `comment` (string)

### Example (target)
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
    }
  ]
}
```

## 5) Config schema design & backward compatibility

### Top-level additions
- New top-level field: `layers` (array)
- Legacy top-level fields remain supported:
  - `lot`, `pip`, `tp1_pips`, `tp2_pips`, `sl_buffer`, `emergency_sl_pips`, `monitor_interval`, `telegram_test_mode`, `source_chat_id`

### Backward-compatible loading contract
- If `layers` is missing:
  - System must behave as today (legacy single-settings behavior).
- If `layers` exists but is empty:
  - System must be safe and predictable (for future runtime phases, it may mean “place no layered orders”).

### Transition contract (documentation)
- Legacy fields should remain in config even when `layers` is present.
- UI should not overwrite legacy fields with layer values; both must remain distinct.

## 6) Validation responsibilities (document-only)
This section defines future validation rules. No code is required here.

### Validation phase responsibilities
1. Validate that `layers` is an array when present.
2. For each layer entry:
   - ensure required fields exist or are derived/defaulted in a deterministic way
   - ensure numeric fields are numeric and within allowed bounds
   - ensure booleans are boolean (or convertible safely)
3. Ensure legacy safe fields remain valid (same as current rules).
4. Sanitization policy:
   - only persist a whitelist of safe layer fields
   - do not persist unknown keys (future hardening)

### Constraint examples (conceptual)
- `lot` must be > 0
- `tp_pips` must be > 0 when `tp_enabled=true`
- `be_trigger_pips` and `be_offset_pips` must be >= 0 when `be_enabled=true`

## 7) Clear separation of future runtime phases

### Phase A: UI/config phase
- Create UI to add/remove/edit/enable/disable layer cards.
- Save to `bot_config.json` under `layers`.

### Phase B: Config validation phase
- Validate and sanitize layer config.

### Phase C: Order placement phase (future)
- Bot places orders using active layers.

### Phase D: DB compatibility phase (future)
- DB tracking must associate orders with a layer id/name.

### Phase E: BE monitor phase (future)
- Break-even logic must handle per-layer settings.

## 8) Non-functional design requirements
- Restart disabled behavior must remain unchanged.
- Stop behavior using `run_bot.stop` must remain unchanged.
- Avoid changes to process control routes; only UI/config contract changes are allowed later.


