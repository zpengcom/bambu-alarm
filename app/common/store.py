"""
Shared, file-backed storage for config and live printer state.

The web app (writer) and the MCP server (reader) both import this module.
Data lives under /data (a bind mount) so it survives container rebuilds.
"""

import copy
import json
import os
import tempfile
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

DATA_DIR = os.getenv("ALARM_DATA_DIR", os.getenv("H2D_DATA_DIR", "/data"))
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

# All timestamps are stamped in this timezone (default: China Standard Time).
APP_TZ_NAME = os.getenv("APP_TZ", "Asia/Shanghai")
APP_TZ = ZoneInfo(APP_TZ_NAME)

# RLock: read-modify-write sections hold the lock across load+save, and the
# helpers they call take it again.
_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(APP_TZ).isoformat()


DEFAULT_CONFIG = {
    # List of {"name", "ip", "serial", "access_code", "enabled"}
    "printers": [],
    # Which events trigger an outgoing push (the dashboard log records all of them)
    "alert_on_start": False,
    "alert_on_pause": True,
    "alert_on_finish": True,
    "alert_on_failed": True,
    "alert_on_error_code": True,
    # Custom meanings for error codes, one per line: "code=description"
    "error_code_map": "# 每行一条：错误代码=说明，例如\n83918991=摄像头清理暂停",
    # Telegram
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_api_base": "https://api.telegram.org",
    # Bark (iOS)
    "bark_enabled": False,
    "bark_server": "https://api.day.app",
    "bark_key": "",
    # ntfy
    "ntfy_enabled": False,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "ntfy_token": "",
    # Any number of generic webhooks (Feishu / WeCom / DingTalk bots, n8n, ...):
    # list of {"name", "enabled", "method", "url", "headers", "body_template"}
    "webhooks": [],
}

DEFAULT_PRINTER_STATE = {
    "connected": False,
    "gcode_state": None,   # None = never seen (first sighting is a silent baseline)
    "print_error": None,
    "task_name": None,
    "progress": None,
    "last_seen": None,
}

STATE_LABELS = {
    "IDLE": "空闲", "PREPARE": "准备中", "SLICING": "切片中", "RUNNING": "打印中",
    "PAUSE": "已暂停", "FINISH": "已完成", "FAILED": "失败",
}


def _atomic_write(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _migrate_config(data: dict) -> dict:
    """Upgrade configs written by the old single-printer / xiaozhi_* versions."""
    if "printers" not in data and (data.get("printer_ip") or data.get("printer_serial")):
        data["printers"] = [{
            "name": "H2D",
            "ip": data.get("printer_ip", ""),
            "serial": data.get("printer_serial", ""),
            "access_code": data.get("printer_access_code", ""),
            "enabled": True,
        }]
    # Oldest versions called the single webhook "xiaozhi_*".
    for old, new in (
        ("xiaozhi_enabled", "webhook_enabled"), ("xiaozhi_method", "webhook_method"),
        ("xiaozhi_url", "webhook_url"), ("xiaozhi_headers", "webhook_headers"),
        ("xiaozhi_body_template", "webhook_body_template"),
    ):
        if old in data and new not in data:
            data[new] = data[old]
    # Single webhook_* settings -> first entry of the webhooks list.
    if "webhooks" not in data and (data.get("webhook_url") or data.get("webhook_enabled")):
        data["webhooks"] = [{
            "name": "Webhook",
            "enabled": bool(data.get("webhook_enabled")),
            "method": data.get("webhook_method") or "POST",
            "url": data.get("webhook_url", ""),
            "headers": data.get("webhook_headers", ""),
            "body_template": data.get("webhook_body_template") or '{"text": "{message}"}',
        }]
    return data


def load_config() -> dict:
    with _lock:
        merged = copy.deepcopy(DEFAULT_CONFIG)
        merged.update(_migrate_config(_read_json(CONFIG_PATH)))
        return merged


def save_config(config: dict):
    with _lock:
        merged = copy.deepcopy(DEFAULT_CONFIG)
        merged.update(config)
        _atomic_write(CONFIG_PATH, merged)


def load_state() -> dict:
    with _lock:
        data = _read_json(STATE_PATH)
        return {
            "printers": data.get("printers", {}) if isinstance(data.get("printers"), dict) else {},
            "alerts": data.get("alerts", []) if isinstance(data.get("alerts"), list) else [],
        }


def _mutate(fn):
    with _lock:
        state = load_state()
        fn(state)
        _atomic_write(STATE_PATH, state)
        return state


def get_printer_state(serial: str) -> dict:
    merged = dict(DEFAULT_PRINTER_STATE)
    merged.update(load_state()["printers"].get(serial, {}))
    return merged


def update_printer_state(serial: str, **kwargs):
    def fn(state):
        entry = state["printers"].setdefault(serial, {})
        entry.update(kwargs)
    return _mutate(fn)


def add_alert(title: str, message: str, printer: str = "", max_alerts: int = 50):
    def fn(state):
        state["alerts"].insert(0, {
            "time": now_iso(), "title": title, "message": message, "printer": printer,
        })
        state["alerts"] = state["alerts"][:max_alerts]
    return _mutate(fn)


def clear_alerts():
    def fn(state):
        state["alerts"] = []
    return _mutate(fn)


def parse_error_code_map(text: str) -> dict:
    """Parse lines like '83918991=摄像头清理暂停' (also accepts ':' / '：').
    Codes may be decimal or 0x-hex. Blank, '#' and invalid lines are skipped."""
    result = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", "：", ":"):
            if sep in line:
                code_str, desc = line.split(sep, 1)
                break
        else:
            continue
        try:
            code = int(code_str.strip(), 0)
        except ValueError:
            continue
        desc = desc.strip()
        if desc:
            result[code] = desc
    return result


def printer_overview(config: dict = None, state: dict = None) -> list:
    """One merged, secret-free status record per configured printer."""
    config = config or load_config()
    state = state or load_state()
    code_map = parse_error_code_map(config.get("error_code_map", ""))
    rows = []
    for p in config.get("printers", []):
        s = dict(DEFAULT_PRINTER_STATE)
        s.update(state["printers"].get(p.get("serial", ""), {}))
        code = s["print_error"] or 0
        rows.append({
            "name": p.get("name") or "打印机",
            "enabled": p.get("enabled", True),
            "connected": bool(s["connected"]),
            "gcode_state": s["gcode_state"],
            "state_text": STATE_LABELS.get(s["gcode_state"], s["gcode_state"] or "未知"),
            "print_error": code,
            "error_text": code_map.get(code, "") if code else "",
            "task_name": s["task_name"],
            "progress": s["progress"],
            "last_seen": s["last_seen"],
        })
    return rows
