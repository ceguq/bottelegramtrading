# OpenSpec Tasks: implement-configurable-trading-layers

## Overview
Tasks are written as small phases with a read-only approach for this OpenSpec phase.

## Phase 1: read-only audit
- [ ] Review current localhost control panel config flow (GET render + POST save routes)
- [ ] Review current safe-field whitelist behavior in the panel (legacy keys)
- [ ] Review current config loading/validation behavior in `bot_settings.py` (legacy strict parsing)
- [ ] Review current bot start/stop process control behavior (restart disabled requirement)
- [ ] Identify where future layer config will be stored in `bot_config.json`

**Done criteria:** Documentation notes capture all existing constraints without proposing runtime changes.

## Phase 2: config schema design
- [ ] Define the target `layers` schema contract (array of layer objects)
- [ ] Define recommended layer fields and their types (enabled, lot, tp, be, comment)
- [ ] Specify backward compatibility when `layers` is missing
- [ ] Specify persistence rules: must not remove/alter existing legacy fields
- [ ] Specify sanitization/whitelist policy (future)

**Done criteria:** Schema contract documented with examples and backward compatibility rules.

## Phase 3: panel UI add/remove layer
- [ ] Specify UI section requirements: “Layer Settings”
- [ ] Specify add action: “+ Tambah Layer”
- [ ] Specify per-layer editable card/detail layout
- [ ] Specify enable/disable toggle per layer
- [ ] Specify remove layer action per card
- [ ] Specify save behavior: writes `layers` into `bot_config.json`

**Done criteria:** UX behavior is fully described without implementing UI code.

## Phase 4: bot_settings validation
- [ ] Define validation responsibilities for layer objects (future)
- [ ] Define constraints based on enabled flags (tp_enabled/be_enabled)
- [ ] Define backward compatibility: legacy fields validate as today when `layers` missing
- [ ] Define error reporting strategy for invalid layer fields (future)

**Done criteria:** Validation rules are clear enough to implement later without ambiguity.

## Phase 5: order placement from layers
- [ ] Document how future order placement phase should consume active layers
- [ ] Explicitly state that order placement is out-of-scope for this OpenSpec phase
- [ ] Define what “active layer” means (enabled=true)
- [ ] Define how layer settings map to order parameters (conceptually)

**Done criteria:** Clear separation is documented; no runtime changes are proposed beyond mapping notes.

## Phase 6: DB tracking compatibility
- [ ] Document the required DB association strategy for layered orders (future)
- [ ] Define how to store layer identity in DB (id/name/comment), without changing schema now
- [ ] Identify DB compatibility risks (missing layer data, empty layers, removed layers)

**Done criteria:** DB compatibility considerations documented for later implementation.

## Phase 7: BE monitor compatibility
- [ ] Document how future BE monitor should interpret per-layer be settings (future)
- [ ] Define expected behavior when be_enabled=false
- [ ] Define how BE trigger/offset should be evaluated per layer (conceptual)

**Done criteria:** BE monitor mapping and constraints documented.

## Phase 8: post-edit audit
- [ ] Ensure OpenSpec files reflect the requested phase separation and safety notes
- [ ] Ensure no runtime file changes were made during this phase
- [ ] Ensure backward compatibility requirements are explicitly stated
- [ ] Ensure Start/Stop/restart constraints are preserved in documentation

**Done criteria:** Documentation is internally consistent and meets acceptance criteria.

## Phase 9: manual git review
- [ ] Confirm only OpenSpec documentation files are added/changed
- [ ] Verify no runtime logic files were modified
- [ ] Verify no runtime scripts were executed
- [ ] Review the final diff manually

**Done criteria:** Human-readable confirmation that changes are doc-only.

