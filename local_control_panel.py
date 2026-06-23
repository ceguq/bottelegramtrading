import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import os
import subprocess
import time
import signal



BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765

# Safe keys only
SAFE_KEYS = [
    "lot",
    "pip",
    "tp1_pips",
    "tp2_pips",
    "sl_buffer",
    "emergency_sl_pips",
    "monitor_interval",
    "telegram_test_mode",
    "source_chat_id",
]


def _safe_load_bot_config_json() -> dict:
    config_path = BASE_DIR / "bot_config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _safe_extract_allowed_keys(obj: dict) -> dict:
    return {k: obj.get(k) for k in SAFE_KEYS if k in obj}


def _string_to_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise ValueError("telegram_test_mode must be boolean")


def _parse_number(value, *, field_name: str, allow_zero: bool = False) -> float:
    try:
        if isinstance(value, str):
            v = value.strip()
            num = float(v)
        else:
            num = float(value)
    except Exception as e:
        raise ValueError(f"{field_name} must be numeric") from e

    if allow_zero:
        if num < 0:
            raise ValueError(f"{field_name} must be >= 0")
    else:
        if num <= 0:
            raise ValueError(f"{field_name} must be > 0")

    return num


def _validate_and_build_settings_from_input(input_obj: dict) -> dict:
    """Validate Phase 3B fields and return a sanitized config (only SAFE_KEYS)."""

    errors = []

    def get(k):
        return input_obj.get(k)

    # lot: numeric > 0
    try:
        lot = _parse_number(get("lot"), field_name="lot", allow_zero=False)
    except ValueError as e:
        errors.append(str(e))
        lot = None

    # pip: numeric > 0
    try:
        pip = _parse_number(get("pip"), field_name="pip", allow_zero=False)
    except ValueError as e:
        errors.append(str(e))
        pip = None

    # tp1_pips: numeric > 0
    try:
        tp1_pips = _parse_number(get("tp1_pips"), field_name="tp1_pips", allow_zero=False)
    except ValueError as e:
        errors.append(str(e))
        tp1_pips = None

    # tp2_pips: numeric > 0 and >= tp1_pips
    try:
        tp2_pips = _parse_number(get("tp2_pips"), field_name="tp2_pips", allow_zero=False)
    except ValueError as e:
        errors.append(str(e))
        tp2_pips = None

    # sl_buffer: numeric >= 0
    try:
        sl_buffer = _parse_number(get("sl_buffer"), field_name="sl_buffer", allow_zero=True)
    except ValueError as e:
        errors.append(str(e))
        sl_buffer = None

    # emergency_sl_pips: numeric > 0
    try:
        emergency_sl_pips = _parse_number(
            get("emergency_sl_pips"), field_name="emergency_sl_pips", allow_zero=False
        )
    except ValueError as e:
        errors.append(str(e))
        emergency_sl_pips = None

    # monitor_interval: numeric >= 1
    try:
        monitor_interval = _parse_number(
            get("monitor_interval"), field_name="monitor_interval", allow_zero=False
        )
        if monitor_interval < 1:
            raise ValueError("monitor_interval must be >= 1")
    except ValueError as e:
        errors.append(str(e))
        monitor_interval = None

    # telegram_test_mode: boolean
    try:
        telegram_test_mode_val = get("telegram_test_mode")
        if isinstance(telegram_test_mode_val, bool):
            telegram_test_mode = telegram_test_mode_val
        else:
            telegram_test_mode = _string_to_bool(str(telegram_test_mode_val))
    except ValueError as e:
        errors.append(str(e))
        telegram_test_mode = None

    # source_chat_id: integer
    try:
        sid_val = get("source_chat_id")
        if isinstance(sid_val, bool) or sid_val is None:
            raise ValueError("source_chat_id must be integer")
        if isinstance(sid_val, str):
            sid = sid_val.strip()
        else:
            sid = str(sid_val)
        source_chat_id = int(sid)
    except Exception:
        errors.append("source_chat_id must be integer")
        source_chat_id = None

    # tp2_pips constraint: >= tp1_pips
    if tp1_pips is not None and tp2_pips is not None and tp2_pips < tp1_pips:
        errors.append("tp2_pips must be >= tp1_pips")

    if errors:
        raise ValueError("; ".join(errors))

    return {
        "lot": lot,
        "pip": pip,
        "tp1_pips": tp1_pips,
        "tp2_pips": tp2_pips,
        "sl_buffer": sl_buffer,
        "emergency_sl_pips": emergency_sl_pips,
        "monitor_interval": monitor_interval,
        "telegram_test_mode": telegram_test_mode,
        "source_chat_id": source_chat_id,
    }


def _write_bot_config_with_backup(new_config: dict) -> None:
    cfg_path = BASE_DIR / "bot_config.json"
    backup_path = BASE_DIR / "bot_config.backup.json"
    tmp_path = BASE_DIR / "bot_config.tmp.json"

    prev = _safe_load_bot_config_json()
    prev_dict = dict(prev) if isinstance(prev, dict) else {}

    merged = dict(prev_dict)
    merged.update(_safe_extract_allowed_keys(new_config))

    backup_path.write_text(
        json.dumps(prev_dict, indent=2) + "\n",
        encoding="utf-8",
    )

    tmp_path.write_text(
        json.dumps(merged, indent=2) + "\n",
        encoding="utf-8",
    )

    tmp_path.replace(cfg_path)


def _build_sanitized_config_api() -> dict:
    from bot_settings import load_settings, settings_to_dict

    settings = load_settings()
    return settings_to_dict(settings)


def _html_escape(s) -> str:
    return html.escape(str(s), quote=True)


def _layer_value_to_string(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _render_mock_layer_card() -> str:
    return (
        '<div style="margin-top:10px;padding:10px;border-radius:8px;border:1px solid #e9ecef;background:#fff;">'
        '<div style="font-weight:800;margin-bottom:6px;">Layer Settings - Layer 1 (Mock, disabled)</div>'

        '<div style="margin-top:8px;color:#333;line-height:1.6;">'
        '<div style="display:flex;gap:12px;align-items:center;margin-bottom:6px;">'
        '<input type="checkbox" disabled checked /> <span>enabled</span>'
        '<span style="color:#666;">&nbsp;</span>'
        '</div>'

        '<div style="margin-bottom:6px;">name</div>'
        '<div style="margin-bottom:10px;">'
        '<input type="text" disabled value="L1" />'
        '</div>'

        '<div style="margin-bottom:6px;">lot</div>'
        '<div style="margin-bottom:10px;">'
        '<input type="text" disabled value="" placeholder="@@LOT@@" />'
        '</div>'

        '<div style="display:flex;gap:12px;align-items:center;margin-bottom:6px;">'
        '<input type="checkbox" disabled checked /> <span>tp_enabled</span>'
        '</div>'

        '<div style="margin-bottom:6px;">tp_pips</div>'
        '<div style="margin-bottom:10px;">'
        '<input type="text" disabled value="" placeholder="@@TP1_PIPS@@" />'
        '</div>'

        '<div style="display:flex;gap:12px;align-items:center;margin-bottom:6px;">'
        '<input type="checkbox" disabled /> <span>be_enabled</span>'
        '</div>'

        '<div style="margin-bottom:6px;">be_trigger_pips</div>'
        '<div style="margin-bottom:10px;">'
        '<input type="text" disabled value="50" />'
        '</div>'

        '<div style="margin-bottom:6px;">be_offset_pips</div>'
        '<div style="margin-bottom:10px;">'
        '<input type="text" disabled value="0" />'
        '</div>'

        '<div style="margin-bottom:6px;">comment</div>'
        '<div style="margin-bottom:10px;">'
        '<input type="text" disabled value="TG-L1" />'
        '</div>'

        '<div style="margin-top:10px;color:#856404;background:#fff3cd;border:1px solid #ffeeba;padding:10px;border-radius:8px;">'
        'Input ini masih disabled. Penyimpanan layer akan dibuat pada phase berikutnya.'
        '</div>'
        '</div>'
        '</div>'
    )


def _render_layer_card(layer: dict[str, Any], idx: int) -> str:
    def row(label: str, value: Any) -> str:
        return (
            '<div style="margin-bottom:6px;font-weight:700;">'
            f'{_html_escape(label)}'
            '</div>'
            '<div style="margin-bottom:12px;color:#333;">'
            f'{_html_escape(_layer_value_to_string(value))}'
            '</div>'
        )

    return (
        '<div style="margin-top:10px;padding:12px;border-radius:8px;border:1px solid #e9ecef;background:#fff;">'
        f'<div style="font-weight:800;margin-bottom:6px;">Layer Settings - Layer {_html_escape(str(idx))}</div>'
        '<div style="margin-top:8px;color:#333;line-height:1.6;">'
        + row("name", layer.get("name", ""))
        + row("enabled", layer.get("enabled", ""))
        + row("lot", layer.get("lot", ""))
        + row("tp_enabled", layer.get("tp_enabled", ""))
        + row("tp_pips", layer.get("tp_pips", ""))
        + row("be_enabled", layer.get("be_enabled", ""))
        + row("be_trigger_pips", layer.get("be_trigger_pips", ""))
        + row("be_offset_pips", layer.get("be_offset_pips", ""))
        + row("comment", layer.get("comment", ""))
        + '</div>'
        + '</div>'
    )


def _render_layers_section(raw_cfg: dict) -> str:
    raw_layers = raw_cfg.get("layers")
    if not isinstance(raw_layers, list):
        return _render_mock_layer_card()

    cards = []
    for item in raw_layers:
        if not isinstance(item, dict):
            continue
        cards.append(_render_layer_card(item, len(cards) + 1))
        if len(cards) >= 10:
            break

    if not cards:
        return _render_mock_layer_card()

    return "".join(cards)


MAX_LAYERS = 10
LAYER_FIELD_NAMES = (
    "name",
    "enabled",
    "lot",
    "tp_enabled",
    "tp_pips",
    "be_enabled",
    "be_trigger_pips",
    "be_offset_pips",
    "comment",
)


def _parse_layer_bool(value, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{field_name} must be boolean")


def _parse_layer_float(value, field_name: str, *, min_value: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        if isinstance(value, str):
            v = value.strip()
            num = float(v)
        else:
            num = float(value)
    except Exception as e:
        raise ValueError(f"{field_name} must be numeric") from e

    if min_value is not None and num < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}")
    return num


def _parse_layer_int(value, field_name: str, *, min_value: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be integer")
    try:
        if isinstance(value, str):
            v = value.strip()
            if v == "":
                raise ValueError
            num = float(v)
        else:
            num = float(value)
    except Exception as e:
        raise ValueError(f"{field_name} must be integer") from e

    if not float(num).is_integer():
        raise ValueError(f"{field_name} must be integer")

    result = int(num)
    if min_value is not None and result < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}")
    return result


def _validate_layer_item(raw_layer: dict, idx: int) -> dict:
    if not isinstance(raw_layer, dict):
        raise ValueError(f"Layer {idx} must be an object")

    name = raw_layer.get("name", "")
    if not isinstance(name, str):
        raise ValueError(f"layers[{idx}].name must be a string")
    name = name.strip()
    if not name:
        raise ValueError(f"layers[{idx}].name must not be empty")
    if len(name) > 40:
        raise ValueError(f"layers[{idx}].name must be at most 40 characters")

    enabled = _parse_layer_bool(raw_layer.get("enabled"), f"layers[{idx}].enabled")
    lot = _parse_layer_float(raw_layer.get("lot"), f"layers[{idx}].lot", min_value=0)
    if lot <= 0:
        raise ValueError(f"layers[{idx}].lot must be > 0")

    tp_enabled = _parse_layer_bool(raw_layer.get("tp_enabled"), f"layers[{idx}].tp_enabled")
    tp_pips = _parse_layer_int(raw_layer.get("tp_pips"), f"layers[{idx}].tp_pips", min_value=0)
    if tp_enabled and tp_pips <= 0:
        raise ValueError(f"layers[{idx}].tp_pips must be > 0 when tp_enabled is true")

    be_enabled = _parse_layer_bool(raw_layer.get("be_enabled"), f"layers[{idx}].be_enabled")
    be_trigger_pips = _parse_layer_int(
        raw_layer.get("be_trigger_pips"), f"layers[{idx}].be_trigger_pips", min_value=0
    )
    be_offset_pips = _parse_layer_int(
        raw_layer.get("be_offset_pips"), f"layers[{idx}].be_offset_pips", min_value=0
    )

    comment = raw_layer.get("comment", "")
    if not isinstance(comment, str):
        raise ValueError(f"layers[{idx}].comment must be a string")
    comment = comment.strip()
    if not comment:
        raise ValueError(f"layers[{idx}].comment must not be empty")
    if len(comment) > 40:
        raise ValueError(f"layers[{idx}].comment must be at most 40 characters")

    # comment is plain metadata only; do not use it for runtime matching yet.
    return {
        "name": name,
        "enabled": enabled,
        "lot": lot,
        "tp_enabled": tp_enabled,
        "tp_pips": tp_pips,
        "be_enabled": be_enabled,
        "be_trigger_pips": be_trigger_pips,
        "be_offset_pips": be_offset_pips,
        "comment": comment,
    }


def _validate_layers_config(raw_layers) -> list[dict]:
    if raw_layers is None:
        return []
    if not isinstance(raw_layers, list):
        raise ValueError("layers must be a list")
    if len(raw_layers) > MAX_LAYERS:
        raise ValueError(f"layers must not contain more than {MAX_LAYERS} items")

    validated_layers = []
    for idx, item in enumerate(raw_layers, start=1):
        validated_layers.append(_validate_layer_item(item, idx))
    return validated_layers


def _render_root_page(*, cfg: dict, layers_html: str = "", error: str = "", saved: bool = False) -> str:
    warning_html = (
        '<div style="padding:10px;background:#fff3cd;border:1px solid #ffeeba;margin:12px 0;">'
        'Perubahan config baru aktif setelah bot direstart.'
        "</div>"
    )

    msg_html = ""
    if saved:
        msg_html = (
            '<div style="padding:10px;background:#d4edda;border:1px solid #c3e6cb;margin:12px 0;">'
            "Config tersimpan."
            "</div>"
        )

    err_html = ""
    if error:
        err_html = (
            '<div style="padding:10px;background:#f8d7da;border:1px solid #f5c6cb;margin:12px 0;color:#721c24;">'
            f"Error: {_html_escape(error)}"
            "</div>"
        )

    def val(k):
        return cfg.get(k, "")

    template_str = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BOT TRADING TELEGRAM CONTROL PANEL</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    table { border-collapse: collapse; width: 100%; max-width: 760px; }
    th, td { border: 1px solid #ddd; padding: 8px; }
    th { background: #f6f6f6; text-align: left; }
    code { background: #f6f6f6; padding: 2px 4px; border-radius: 3px; }
    input[type="text"] { width: 220px; }

    .status-banner{display:flex;align-items:center;gap:12px;padding:12px 14px;margin:12px 0;border-radius:10px;border:1px solid #e9ecef;background:#f8f9fa;}
    .status-light{width:18px;height:18px;border-radius:50%;background:#999;box-shadow:0 0 0 4px rgba(0,0,0,0.03);}
    .status-light.green{background:#28a745;}
    .status-light.red{background:#dc3545;}
    .status-light.yellow{background:#ffc107;}
    .status-banner .status-text{font-weight:800;letter-spacing:0.2px;}
  </style>
</head>
<body>
  <h1>BOT TRADING TELEGRAM CONTROL PANEL</h1>

  @@STATUS_LIGHT_BANNER@@
  @@MSG_HTML@@



  @@ERR_HTML@@
  @@WARNING_HTML@@


  <h2>Config (Safe Fields)</h2>
  <form method="POST" action="/config">
    <table>
      <tr><th>lot</th><td><input type="text" name="lot" value="@@LOT@@" /></td></tr>
      <tr><th>pip</th><td><input type="text" name="pip" value="@@PIP@@" /></td></tr>
      <tr><th>tp1_pips</th><td><input type="text" name="tp1_pips" value="@@TP1_PIPS@@" /></td></tr>
      <tr><th>tp2_pips</th><td><input type="text" name="tp2_pips" value="@@TP2_PIPS@@" /></td></tr>
      <tr><th>sl_buffer</th><td><input type="text" name="sl_buffer" value="@@SL_BUFFER@@" /></td></tr>
      <tr><th>emergency_sl_pips</th><td><input type="text" name="emergency_sl_pips" value="@@EMERGENCY_SL_PIPS@@" /></td></tr>
      <tr><th>monitor_interval</th><td><input type="text" name="monitor_interval" value="@@MONITOR_INTERVAL@@" /></td></tr>
      <tr><th>telegram_test_mode</th><td><input type="text" name="telegram_test_mode" value="@@TELEGRAM_TEST_MODE@@" /> <span style="color:#666;">true/false</span></td></tr>
      <tr><th>source_chat_id</th><td><input type="text" name="source_chat_id" value="@@SOURCE_CHAT_ID@@" /></td></tr>
    </table>

    <p><button type="submit">Simpan</button></p>
  </form>

  <div style="padding:12px;background:#f8f9fa;border:1px solid #e9ecef;border-radius:10px;max-width:760px;margin:14px 0;">
    <div style="font-weight:800;margin-bottom:6px;">Layer Settings</div>
    <div style="color:#333;">
      Fitur konfigurasi layer akan ditambahkan bertahap. Saat ini order execution masih memakai legacy logic.
    </div>

    <div style="margin-top:10px;">
      <button type="button" disabled style="opacity:0.55; cursor:not-allowed;">+ Tambah Layer</button>
    </div>
    <div style="margin-top:6px;color:#666;">
      Tombol ini baru tampilan. Add/remove layer akan diaktifkan pada phase berikutnya.
    </div>

    @@LAYERS_SECTION@@

    <div style="margin-top:10px;padding:10px;border-radius:8px;border:1px solid #e9ecef;background:#f8f9fa;">
      <div style="font-weight:800;margin-bottom:6px;">Field yang nanti bisa diatur</div>
      <ul style="margin:0;padding-left:18px;color:#333;line-height:1.7;">
        <li>Aktif / nonaktif layer</li>
        <li>Nama layer</li>
        <li>Lot per layer</li>
        <li>TP aktif / nonaktif</li>
        <li>TP pips</li>
        <li>BE aktif / nonaktif</li>
        <li>BE trigger pips</li>
        <li>BE offset pips</li>
        <li>Comment order</li>
      </ul>
    </div>

    <div style="margin-top:10px;color:#856404;background:#fff3cd;border:1px solid #ffeeba;padding:10px;border-radius:8px;">
      Layer config belum dipakai untuk order sampai phase berikutnya.
    </div>
  </div>

  <h2>Status</h2>
  <div>
    panel: <code>running</code><br/>
    bot_process_hint: <code>process status only (Phase 3C)</code>
  </div>


<h2>Bot Controls</h2>
  <div style="padding:10px;background:#f8f9fa;border:1px solid #e9ecef;margin:10px 0;">
    <div style="margin-bottom:8px;color:#333;">Phase 3E: safe process control (localhost)</div>
    <button type="button" onclick="botStart()">Start Bot</button>
    <button type="button" onclick="botStop()" style="margin-left:8px;">Stop Bot</button>
    <button type="button" onclick="return false;" id="btnRestart" style="margin-left:8px; opacity:0.55;" disabled>Restart disabled - use Stop then Start</button>

    <div style="margin-top:10px;color:#856404;background:#fff3cd;border:1px solid #ffeeba;padding:10px;">
      Stop Bot uses a safe stop request handled by run_bot.py. No processes are killed by the panel.
    </div>
  @@BOT_PROCESS_SECTION@@

<script>
function _postJson(url) {
  return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
    .then(async (res) => {
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      return { res, data };
    });
}

function _showMsg(ok, message) {
  const el = document.getElementById('botActionMsg');
  if (!el) return;
  el.style.color = ok ? '#155724' : '#721c24';
  el.textContent = message || (ok ? 'Success' : 'Failed');
}

async function _refreshStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return window.location.reload();
  } catch (e) {
    return window.location.reload();
  }
  window.location.reload();
}

async function botStart() {
  _showMsg(true, 'Starting bot...');
  const { res, data } = await _postJson('/bot/start');
  const ok = data && data.ok === true;
  const msg = data && data.message ? data.message : ('HTTP ' + res.status);
  _showMsg(ok, msg);
  if (ok) await _refreshStatus(); else await _refreshStatus();
}

async function botStop() {
  _showMsg(true, 'Stopping bot...');
  const { res, data } = await _postJson('/bot/stop');
  const ok = data && data.ok === true;
  const msg = data && data.message ? data.message : ('HTTP ' + res.status);
  _showMsg(ok, msg);
  if (ok) await _refreshStatus(); else await _refreshStatus();
}

async function botRestart() {
  // Disabled for Phase 3D. Button is also rendered as disabled.
  _showMsg(false, 'Restart is temporarily disabled. Use Stop then Start.');
}

</script>

</body>
</html>"""



    bot_process_section_html = ""

    # Build Bot Process Status section safely (no crash if inspection fails)
    try:
        status = _compute_bot_stack_status()
        bot_stack_running = bool(status.get("bot_stack_running", False))
        run_bot_running = bool(status.get("run_bot_running", False))
        telegram_listener_running = bool(status.get("telegram_listener_running", False))
        be_monitor_running = bool(status.get("be_monitor_running", False))
        process_count = int(status.get("process_count", 0))
        processes = status.get("processes", []) if isinstance(status.get("processes"), list) else []
        note_html = (
            '<div style="padding:8px;background:#f8f9fa;border:1px solid #e9ecef;margin:10px 0;">'
            'Windows venv may show wrapper processes; this panel groups by script name.'
            '</div>'
        )

        summary_html = (
            '<div>'
            f'bot_stack_running: <code>{"true" if bot_stack_running else "false"}</code><br/>'
            f'run_bot.py: <code>{"true" if run_bot_running else "false"}</code><br/>'
            f'telegram_listener.py: <code>{"true" if telegram_listener_running else "false"}</code><br/>'
            f'be_monitor.py: <code>{"true" if be_monitor_running else "false"}</code><br/>'
            f'process_count: <code>{process_count}</code>'
            '</div>'
        )

        if status.get("error"):
            err_html_local = (
                '<div style="padding:10px;background:#f8d7da;border:1px solid #f5c6cb;margin:12px 0;color:#721c24;">'
                f'Error: {_html_escape(str(status.get("error")))}'
                '</div>'
            )
            table_html = ""
        else:
            err_html_local = ""
            if processes:
                rows = []
                for p in processes:
                    try:
                        rows.append(
                            "<tr>"
                            f"<td>{_html_escape(p.get('process_id', ''))}</td>"
                            f"<td>{_html_escape(p.get('parent_process_id', ''))}</td>"
                            f"<td>{_html_escape(p.get('script', ''))}</td>"
                            f"<td>{_html_escape(p.get('executable_kind', ''))}</td>"
                            f"<td>{_html_escape(p.get('command_hint', ''))}</td>"
                            "</tr>"
                        )
                    except Exception:
                        continue

                table_html = (
                    '<table>'
                    '<tr><th>process_id</th><th>parent_process_id</th><th>script</th><th>executable_kind</th><th>command_hint</th></tr>'
                    + "".join(rows)
                    + '</table>'
                )
            else:
                table_html = '<div style="color:#666;">No matching bot processes found.</div>'

        bot_process_section_html = note_html + summary_html + err_html_local + table_html

    except Exception as e:
        bot_process_section_html = (
            '<div style="padding:10px;background:#f8d7da;border:1px solid #f5c6cb;margin:12px 0;color:#721c24;">'
            f'Error: {_html_escape(str(e) or "process inspection failed")}'
            '</div>'
        )

    # Manual placeholder replacement (no string.Template dependency)
    # Bot status light logic (PHASE 3F UI only)
    try:
        status = _compute_bot_stack_status()
        bot_stack_running = bool(status.get("bot_stack_running", False))
        run_bot_running = bool(status.get("run_bot_running", False))
        telegram_listener_running = bool(status.get("telegram_listener_running", False))
        be_monitor_running = bool(status.get("be_monitor_running", False))
        any_process_running = bool(run_bot_running or telegram_listener_running or be_monitor_running)

        if bot_stack_running:
            light_class = "green"
            status_text = "BOT RUNNING"
        else:
            if not any_process_running:
                light_class = "red"
                status_text = "BOT STOPPED"
            else:
                light_class = "yellow"
                status_text = "BOT PARTIAL / CHECK NEEDED"

        status_light_banner_html = (
            '<div class="status-banner">'
            f'<span class="status-light {light_class}"></span>'
            f'<span class="status-text">{_html_escape(status_text)}</span>'
            '</div>'
        )
    except Exception:
        status_light_banner_html = (
            '<div class="status-banner">'
            '<span class="status-light red"></span>'
            '<span class="status-text">BOT STOPPED</span>'
            '</div>'
        )

    replacements = {
        "@@STATUS_LIGHT_BANNER@@": status_light_banner_html,
        "@@MSG_HTML@@": msg_html,
        "@@ERR_HTML@@": err_html,
        "@@WARNING_HTML@@": warning_html,
        "@@BOT_PROCESS_SECTION@@": bot_process_section_html,
        "@@LAYERS_SECTION@@": layers_html,
    
    # also support legacy placeholder location (inside static template)



        "@@LOT@@": _html_escape(val("lot")),
        "@@PIP@@": _html_escape(val("pip")),
        "@@TP1_PIPS@@": _html_escape(val("tp1_pips")),
        "@@TP2_PIPS@@": _html_escape(val("tp2_pips")),
        "@@SL_BUFFER@@": _html_escape(val("sl_buffer")),
        "@@EMERGENCY_SL_PIPS@@": _html_escape(val("emergency_sl_pips")),
        "@@MONITOR_INTERVAL@@": _html_escape(val("monitor_interval")),
        "@@TELEGRAM_TEST_MODE@@": _html_escape(val("telegram_test_mode")),
        "@@SOURCE_CHAT_ID@@": _html_escape(val("source_chat_id")),

        "@@BOT_CONTROLS_SECTION@@": "",
    }



    html_out = template_str
    for k, v in replacements.items():
        html_out = html_out.replace(k, str(v))
    return html_out




def _inspect_bot_processes_windows() -> dict:

    """Inspect bot-related processes (Phase 3C parsing bugfix only).

    Uses PowerShell Get-CimInstance Win32_Process, filters python processes and
    returns sanitized JSON rows.
    """

    import subprocess
    from subprocess import PIPE

    # Return ONLY the required raw properties.
    # Avoid returning PowerShell itself by filtering on exact bot script names.
    # This prevents using the project path alone to select targets.
    ps_cmd = (
        "$procs = Get-CimInstance Win32_Process | Where-Object {"
        "($_.Name -match 'python') -and "
        "(($_.CommandLine -match 'run_bot\\.py') -or ($_.CommandLine -match 'telegram_listener\\.py') -or ($_.CommandLine -match 'be_monitor\\.py'))"
        "};"
        "$procs | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine |"
        "ConvertTo-Json -Compress"
    )

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_cmd,
            ],
            stdout=PIPE,
            stderr=PIPE,
            text=True,
            timeout=6,
            check=False,
        )

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            return {"error": err if err else "process inspection failed"}

        out = (result.stdout or "").strip()
        if not out:
            return {"process_count": 0, "processes": []}

        parsed = json.loads(out)
        if isinstance(parsed, dict):
            rows = [parsed]
        else:
            rows = parsed

        dedupe = set()
        sanitized = []

        for row in rows:
            try:
                process_id = int(row.get("ProcessId"))
                parent_process_id = int(row.get("ParentProcessId"))
            except Exception:
                continue

            executable_path = row.get("ExecutablePath") or ""
            command_line = row.get("CommandLine") or ""

            # Determine script; skip if none matches.
            cmd_lower = command_line.lower()
            if "run_bot.py" in cmd_lower:
                script = "run_bot.py"
            elif "telegram_listener.py" in cmd_lower:
                script = "telegram_listener.py"
            elif "be_monitor.py" in cmd_lower:
                script = "be_monitor.py"
            else:
                # Exclude local_control_panel.py and any other scripts from bot process view.
                continue

            # executable_kind: venv if .venv in path or cmd
            executable_kind = "venv" if (".venv" in executable_path.lower() or ".venv" in cmd_lower) else "global"

            # command_hint: do not show full cmd line
            command_hint = f"{executable_kind} {script}"

            key = (process_id, script, parent_process_id)
            if key in dedupe:
                continue
            dedupe.add(key)

            sanitized.append(
                {
                    "process_id": process_id,
                    "parent_process_id": parent_process_id,
                    "script": script,
                    "executable_kind": executable_kind,
                    "command_hint": command_hint,
                    "command_line": command_line,
                }
            )

        return {"process_count": len(sanitized), "processes": sanitized}

    except Exception as e:
        return {"error": str(e) or "process inspection failed"}



def _compute_bot_stack_status() -> dict:
    """Aggregate running flags based on sanitized process scan."""
    if json is None:  # pragma: no cover
        return {"error": "json unavailable"}

    import platform

    if platform.system().lower().startswith("win"):
        scan = _inspect_bot_processes_windows()
    else:
        scan = {"error": "process inspection is supported on Windows only"}

    if "error" in scan:
        return {
            "process_count": 0,
            "processes": [],
            "run_bot_running": False,
            "telegram_listener_running": False,
            "be_monitor_running": False,
            "bot_stack_running": False,
            "error": scan.get("error"),
        }

    processes = scan.get("processes", [])
    scripts = [p.get("script") for p in processes]

    run_bot_running = any(s == "run_bot.py" for s in scripts)
    telegram_listener_running = any(s == "telegram_listener.py" for s in scripts)
    be_monitor_running = any(s == "be_monitor.py" for s in scripts)
    bot_stack_running = run_bot_running and telegram_listener_running and be_monitor_running

    return {
        "run_bot_running": bool(run_bot_running),
        "telegram_listener_running": bool(telegram_listener_running),
        "be_monitor_running": bool(be_monitor_running),
        "bot_stack_running": bool(bot_stack_running),
        "process_count": int(scan.get("process_count", len(processes))),
        "processes": processes,
    }


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler._send(status_code, "application/json; charset=utf-8", body)


def _bot_stack_pids_strict() -> list[int]:
    """Return targeted PIDs for the Phase 3D process control.

    Strictly uses existing inspection and only returns PIDs for the exact
    project scripts: run_bot.py, telegram_listener.py, be_monitor.py.

    Does NOT use broad python process matching.
    """
    status = _compute_bot_stack_status()
    if status.get("error"):
        return []

    pids: list[int] = []
    for p in status.get("processes", []) or []:
        script = p.get("script")
        if script in {"run_bot.py", "telegram_listener.py", "be_monitor.py"}:
            try:
                pids.append(int(p.get("process_id")))
            except Exception:
                continue

    # de-dupe
    return list(dict.fromkeys(pids))


def _terminate_pids(pids: list[int], *, timeout_seconds: float = 3.0) -> dict:
    """Try graceful termination then force only targeted PIDs."""

    if not pids:
        return {"ok": True, "terminated": [], "forced": []}

    terminated: list[int] = []
    forced: list[int] = []

    # Graceful attempt
    for pid in pids:
        try:
            if os.name == "nt":
                os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.kill(pid, signal.SIGINT)  # type: ignore[attr-defined]
            terminated.append(pid)
        except Exception:
            # If graceful fails immediately, we'll attempt force later.
            pass

    deadline = time.monotonic() + timeout_seconds
    remaining: set[int] = set(pids)

    while time.monotonic() < deadline:
        still_alive: set[int] = set()
        for pid in remaining:
            try:
                # signal 0 probes existence on POSIX; on Windows it may raise.
                if os.name != "nt":
                    os.kill(pid, 0)  # type: ignore[arg-type]
                else:
                    # Windows: best-effort check via kill with 0 is unsupported; just keep.
                    # We'll rely on next force pass.
                    still_alive.add(pid)
            except Exception:
                continue
            else:
                still_alive.add(pid)

        remaining = still_alive
        if not remaining:
            break
        time.sleep(0.2)

    # Force only remaining
    for pid in list(remaining):
        try:
            if os.name == "nt":
                os.kill(pid, signal.SIGTERM)  # type: ignore[attr-defined]
            else:
                os.kill(pid, signal.SIGTERM)  # type: ignore[attr-defined]
            forced.append(pid)
        except Exception:
            continue

    return {"ok": True, "terminated": terminated, "forced": forced}


def _start_bot_stack() -> dict:
    """Start only run_bot.py using the project venv python.

    Non-blocking: uses subprocess.Popen.
    Redirects stdout/stderr to run_bot.panel.log.
    
    Removes stale run_bot.stop before starting to ensure clean startup.
    """
    status_before = _compute_bot_stack_status()
    if status_before.get("bot_stack_running") is True:
        return {"ok": False, "http_code": 409, "message": "Bot stack already running", "bot_status": status_before}

    # Clean up stale stop request file before starting
    stop_request_path = BASE_DIR / "run_bot.stop"
    try:
        if stop_request_path.exists():
            stop_request_path.unlink()
    except Exception:
        pass

    venv_python = (BASE_DIR / ".venv" / "Scripts" / "python.exe").resolve()
    script_path = (BASE_DIR / "run_bot.py").resolve()

    if not venv_python.exists():
        return {"ok": False, "http_code": 500, "message": "Venv python not found", "bot_status": status_before}
    if not script_path.exists():
        return {"ok": False, "http_code": 500, "message": "run_bot.py not found", "bot_status": status_before}

    log_path = BASE_DIR / "run_bot.panel.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Start non-blocking.
    # Use cwd=BASE_DIR and the venv python only.
    with open(log_path, "ab") as f:
        proc = subprocess.Popen(
            [str(venv_python), str(script_path)],
            cwd=str(BASE_DIR),
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            close_fds=True,
        )

    # Wait briefly then recompute status.
    time.sleep(1.0)
    status_after = _compute_bot_stack_status()

    return {"ok": True, "http_code": 200, "message": "Bot stack start requested", "bot_status": status_after, "pid": proc.pid}


def _stop_bot_stack() -> dict:
    """Request bot stack to stop via stop request file.

    Safe approach: does NOT kill any PIDs directly.
    Instead, writes a stop request file that run_bot.py monitors.
    run_bot.py gracefully stops its own child processes and removes the file.
    
    This ensures local_control_panel.py never kills itself.
    """
    status_before = _compute_bot_stack_status()

    # If none of the target scripts are running, refuse.
    any_running = bool(
        status_before.get("run_bot_running")
        or status_before.get("telegram_listener_running")
        or status_before.get("be_monitor_running")
    )
    if not any_running:
        return {"ok": False, "http_code": 409, "message": "Bot stack not running", "bot_status": status_before}

    # Write stop request file that run_bot.py monitors
    stop_request_path = BASE_DIR / "run_bot.stop"
    try:
        stop_request_path.write_text("stop_requested\n", encoding="utf-8")
    except Exception as e:
        return {
            "ok": False,
            "http_code": 500,
            "message": f"Failed to write stop request: {str(e)}",
            "bot_status": status_before,
        }

    # Wait briefly and poll status a few times to verify stop is progressing
    for _ in range(5):
        time.sleep(0.5)
        status_poll = _compute_bot_stack_status()
        # If bot has already stopped, return immediately
        any_still_running = bool(
            status_poll.get("run_bot_running")
            or status_poll.get("telegram_listener_running")
            or status_poll.get("be_monitor_running")
        )
        if not any_still_running:
            return {"ok": True, "http_code": 200, "message": "Bot stop requested and confirmed stopped", "bot_status": status_poll}

    # Bot still running, but stop request was sent successfully
    status_after = _compute_bot_stack_status()
    return {
        "ok": True,
        "http_code": 200,
        "message": "Bot stop requested; shutdown still in progress",
        "bot_status": status_after,
    }



def _restart_bot_stack() -> dict:
    stop_resp = _stop_bot_stack()
    # If stop refused due to not running, follow spec: restart uses same stop helper.
    if not stop_resp.get("ok"):
        return stop_resp

    time.sleep(0.5)
    return _start_bot_stack()



class LocalControlPanelHandler(BaseHTTPRequestHandler):
    server_version = "LocalControlPanel/3C"


    def _send(self, status_code: int, content_type: str, body: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            try:
                self._handle_root()
            except Exception as e:
                # Absolute fallback: ensure GET / never closes without response.
                try:
                    self._send_root_error_response(e, route="/")
                except Exception:
                    pass
            return

        if path == "/api/config":
            self._handle_api_config()
            return
        if path == "/api/status":
            self._handle_api_status()
            return

        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/config":
            self._handle_post_config_form()
            return
        if path == "/api/config":
            self._handle_post_api_config_json()
            return

        # Phase 3D process control routes (JSON only, localhost only)
        if path in {"/bot/start", "/api/bot/start"}:
            self._handle_post_bot_action("start")
            return
        if path in {"/bot/stop", "/api/bot/stop"}:
            self._handle_post_bot_action("stop")
            return
        if path in {"/bot/restart", "/api/bot/restart"}:
            # Phase 3D: restart is temporarily disabled to avoid unstable state.
            self._handle_post_bot_action("restart_disabled")
            return


        self._send(404, "text/plain; charset=utf-8", b"Not found")


    def _send_root_error_response(self, exc: Exception, *, route: str = "/") -> None:
        """Send a safe 500 HTML response for GET / and log diagnostic info.

        Must not leak credentials or command lines.
        """

        try:
            log_path = BASE_DIR / "local_control_panel.error.log"
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

            # Sanitize message: never log raw CommandLine or PowerShell command strings.
            exc_type = type(exc).__name__
            exc_msg = str(exc)
            exc_msg_sanitized = exc_msg

            # Truncate to reduce sensitive payload exposure.
            if len(exc_msg_sanitized) > 300:
                exc_msg_sanitized = exc_msg_sanitized[:300] + "…"

            # Sanitize traceback: remove any lines that look like PowerShell command execution.
            tb = ""
            try:
                import traceback as _traceback

                tb_raw = _traceback.format_exc()
                tb_lines = []
                for line in tb_raw.splitlines():
                    l = line.lower()
                    if "powershell" in l or "commandline" in l or "-command" in l:
                        continue
                    tb_lines.append(line)
                tb = "\n".join(tb_lines)
            except Exception:
                tb = "<traceback unavailable>"

            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    "{ts} route={route} exc_type={exc_type} exc_msg={exc_msg}\n{tb}\n".format(
                        ts=ts,
                        route=route,
                        exc_type=exc_type,
                        exc_msg=exc_msg_sanitized.replace("\n", " "),
                        tb=tb,
                    )
                )
        except Exception:
            # Never allow logging failures to break the HTTP response.
            pass

        err_type = type(exc).__name__
        err_msg = str(exc)
        if len(err_msg) > 200:
            err_msg = err_msg[:200] + "…"

        safe_err_type = _html_escape(err_type)
        safe_err_msg = _html_escape(err_msg)

        body = (
            "<html><head><meta charset='utf-8'/></head>"
            "<body>"
            "<h2>Root page render error</h2>"
            "<div>"
            f"<div>{safe_err_type}: {safe_err_msg}</div>"
            "</div>"
            "</body></html>"
        )

        try:
            self._send(500, "text/html; charset=utf-8", body.encode("utf-8"))
        except Exception:
            # Final fallback: if headers/body fail, do nothing rather than crash.
            return

    def _handle_root(self):
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            saved = "saved" in parse_qs(parsed.query)
            raw_cfg = _safe_load_bot_config_json()
            cfg = _safe_extract_allowed_keys(raw_cfg)
            layers_html = _render_layers_section(raw_cfg)
            html_page = _render_root_page(cfg=cfg, layers_html=layers_html, saved=saved)
            self._send(200, "text/html; charset=utf-8", html_page.encode("utf-8"))
        except Exception as e:
            return self._send_root_error_response(e, route=route or "/")



    def _handle_post_config_form(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        body_str = raw.decode("utf-8", errors="replace")

        parsed = parse_qs(body_str, keep_blank_values=True)
        input_obj = {k: (v[0] if isinstance(v, list) and v else "") for k, v in parsed.items()}

        raw_cfg = _safe_load_bot_config_json()
        prev_cfg = _safe_extract_allowed_keys(raw_cfg)
        layers_html = _render_layers_section(raw_cfg)

        try:
            validated = _validate_and_build_settings_from_input(input_obj)
            _write_bot_config_with_backup(validated)
            self.send_response(302)
            self.send_header("Location", "/?saved=1")
            self.end_headers()
        except Exception as e:
            html = _render_root_page(cfg=prev_cfg, layers_html=layers_html, error=str(e), saved=False)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _handle_api_config(self):
        try:
            data = _build_sanitized_config_api()
            body = json.dumps(data, separators=(",", ":")).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)}, separators=(",", ":")).encode("utf-8")
            self._send(500, "application/json; charset=utf-8", body)

    def _handle_post_api_config_json(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_length) if content_length > 0 else b""

        try:
            input_obj = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            body = json.dumps({"ok": False, "error": "Invalid JSON"}).encode("utf-8")
            self._send(400, "application/json; charset=utf-8", body)
            return

        if not isinstance(input_obj, dict):
            body = json.dumps({"ok": False, "error": "JSON body must be an object"}).encode("utf-8")
            self._send(400, "application/json; charset=utf-8", body)
            return

        try:
            validated = _validate_and_build_settings_from_input(input_obj)
            _write_bot_config_with_backup(validated)
            resp = _safe_extract_allowed_keys(validated)
            body = json.dumps(resp, separators=(",", ":")).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
            self._send(400, "application/json; charset=utf-8", body)

    def _handle_post_bot_action(self, action: str):
        try:
            if action == "start":
                resp = _start_bot_stack()
            elif action == "stop":
                resp = _stop_bot_stack()
            elif action == "restart":
                resp = {
                    "ok": False,
                    "http_code": 409,
                    "message": "Restart is temporarily disabled. Use Stop then Start.",
                    "bot_status": _compute_bot_stack_status(),
                }
            elif action == "restart_disabled":
                resp = {
                    "ok": False,
                    "http_code": 409,
                    "message": "Restart is temporarily disabled. Use Stop then Start.",
                    "bot_status": _compute_bot_stack_status(),
                }
            else:
                resp = {"ok": False, "http_code": 500, "message": "Unknown action"}


            ok = bool(resp.get("ok"))
            http_code = int(resp.get("http_code", 500 if not ok else 200))

            payload = {
                "ok": ok,
                "message": resp.get("message", ""),
                "bot_status": resp.get("bot_status", _compute_bot_stack_status()),
            }

            _json_response(self, http_code, payload)
        except Exception as e:
            payload = {"ok": False, "message": str(e), "bot_status": _compute_bot_stack_status()}
            _json_response(self, 500, payload)

    def _handle_api_status(self):
        # Phase 3C: return only process status information (no config secrets).
        data = {
            "panel": "running",
            "bot_process_hint": "process status only (Phase 3C)",
            "bot_status": {
                "bot_stack_running": False,
                "run_bot_running": False,
                "telegram_listener_running": False,
                "be_monitor_running": False,
                "process_count": 0,
                "processes": [],
            },
        }


        try:
            status = _compute_bot_stack_status()
            data["bot_status"] = {
                "bot_stack_running": status.get("bot_stack_running", False),
                "run_bot_running": status.get("run_bot_running", False),
                "telegram_listener_running": status.get("telegram_listener_running", False),
                "be_monitor_running": status.get("be_monitor_running", False),
                "process_count": status.get("process_count", 0),
                "processes": status.get("processes", []),
            }
            if status.get("error"):
                data["bot_status"]["error"] = str(status.get("error"))
        except Exception as e:
            data["bot_status"] = {
                "bot_stack_running": False,
                "run_bot_running": False,
                "telegram_listener_running": False,
                "be_monitor_running": False,
                "process_count": 0,
                "processes": [],
                "error": str(e) if str(e) else "process inspection failed",
            }

        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        try:
            self._send(200, "application/json; charset=utf-8", body)
        except ConnectionAbortedError:
            # Client disconnected mid-response (safe behavior)
            return




def main():
    server = HTTPServer((HOST, PORT), LocalControlPanelHandler)
    print(f"Local control panel running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

