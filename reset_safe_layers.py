import argparse
import json
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
BOT_CONFIG_PATH = PROJECT_DIR / "bot_config.json"


SAFE_LAYERS = [
    {
        "enabled": True,
        "lot": 0.01,
        "tp_enabled": True,
        "tp_pips": 30,
        "be_enabled": False,
        "be_trigger_pips": 0,
        "be_offset_pips": 0,
        "comment": "TG-TP1",
    },
    {
        "enabled": True,
        "lot": 0.01,
        "tp_enabled": True,
        "tp_pips": 70,
        "be_enabled": False,
        "be_trigger_pips": 0,
        "be_offset_pips": 0,
        "comment": "TG-TP2",
    },
    {
        "enabled": True,
        "lot": 0.01,
        "tp_enabled": False,
        "tp_pips": 0,
        "be_enabled": False,
        "be_trigger_pips": 0,
        "be_offset_pips": 0,
        "comment": "TG-NO-TP",
    },
]


def _load_bot_config() -> dict:
    if not BOT_CONFIG_PATH.exists():
        print(f"ERROR: Missing {BOT_CONFIG_PATH}")
        raise FileNotFoundError(str(BOT_CONFIG_PATH))

    try:
        data = json.loads(BOT_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {BOT_CONFIG_PATH}: {e}")
        raise

    if not isinstance(data, dict):
        raise ValueError("bot_config.json must be a JSON object")

    return data


def _build_safe_changes(prev_cfg: dict) -> dict:
    new_cfg = dict(prev_cfg)

    # telegram_test_mode = true
    new_cfg["telegram_test_mode"] = True

    # remove allow_real_order if present
    if "allow_real_order" in new_cfg:
        new_cfg.pop("allow_real_order", None)

    # layers exactly 3 objects
    new_cfg["layers"] = SAFE_LAYERS

    return new_cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset bot_config.json to conservative safe layer preset.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write bot_config.json (default is dry-run preview only).",
    )
    args = parser.parse_args()

    try:
        prev_cfg = _load_bot_config()
    except Exception:
        return 1

    new_cfg = _build_safe_changes(prev_cfg)

    # Preview
    print("Proposed changes:")
    print("- telegram_test_mode: True")
    print("- allow_real_order: removed" + (" (was present)" if "allow_real_order" in prev_cfg else " (was already absent)"))
    print("- layers: 3 objects with conservative TEST settings")

    if not args.apply:
        print("DRY RUN: no file changed. Use --apply to write bot_config.json.")
        return 0

    try:
        BOT_CONFIG_PATH.write_text(
            json.dumps(new_cfg, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"ERROR: Failed to write {BOT_CONFIG_PATH}: {e}")
        return 1

    print("Safe layer preset applied: TEST MODE on, real order locked, all lots 0.01.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

