"""
Background MQTT watchers for Bambu Lab printers (LAN mode local MQTT).

One PrinterWatcher thread per configured printer; WatcherManager restarts
them all whenever the settings are saved. Each watcher reconnects on any
failure. Works with X1 / P1 / A1 / H2 series - P1/A1 only report changes
after a "pushall" request, so one is sent on every (re)connect.
"""

import json
import logging
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from app.common import store
from app.web import notify

log = logging.getLogger("bambu-alarm.mqtt")

MQTT_PORT = 8883
RECONNECT_DELAY_SECONDS = 5
HEARTBEAT_WRITE_INTERVAL = 15  # seconds between "last_seen" writes when nothing else changed

# new gcode_state -> (config key enabling the push, title suffix, message)
STATE_ALERTS = {
    "RUNNING": ("alert_on_start", "开始打印", "开始了新的打印任务。"),
    "PAUSE": ("alert_on_pause", "打印暂停", "打印已暂停，请检查设备。"),
    "FINISH": ("alert_on_finish", "打印完成", "打印任务已完成。"),
    "FAILED": ("alert_on_failed", "打印失败", "打印失败，请检查设备。"),
}
# States a job can be (re)started from; RUNNING after PAUSE is just a resume.
START_FROM = {"IDLE", "PREPARE", "SLICING", "FINISH", "FAILED"}


class PrinterWatcher:
    def __init__(self, printer: dict):
        self.name = printer.get("name") or "打印机"
        self.ip = printer["ip"].strip()
        self.serial = printer["serial"].strip()
        self.access_code = printer["access_code"].strip()

        persisted = store.get_printer_state(self.serial)
        self.gcode_state = persisted["gcode_state"]
        self.print_error = persisted["print_error"]
        self.task_name = persisted["task_name"]
        self.progress = persisted["progress"]

        self._stop_event = threading.Event()
        self._thread = None
        self._client = None
        self._last_write = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"watch-{self.serial[-6:]}")
        self._thread.start()

    def signal_stop(self):
        self._stop_event.set()
        client = self._client
        if client:
            try:
                client.disconnect()
            except Exception:
                pass

    def join(self, timeout=5):
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._connect_and_loop()
            except Exception as exc:
                log.error("[%s] MQTT error: %s - retrying in %ss", self.name, exc, RECONNECT_DELAY_SECONDS)
            if not self._stop_event.is_set():
                store.update_printer_state(self.serial, connected=False)
                self._stop_event.wait(RECONNECT_DELAY_SECONDS)
        store.update_printer_state(self.serial, connected=False)

    def _connect_and_loop(self):
        client = mqtt.Client(client_id=f"bambu-alarm-{self.serial[-6:]}", protocol=mqtt.MQTTv311)
        client.username_pw_set("bblp", self.access_code)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE  # the printer uses a self-signed certificate
        client.tls_set_context(ssl_ctx)

        def on_connect(c, userdata, flags, rc, properties=None):
            if rc != 0:
                log.error("[%s] connect refused, rc=%s (check IP / access code)", self.name, rc)
                return
            log.info("[%s] connected to %s:%s", self.name, self.ip, MQTT_PORT)
            c.subscribe(f"device/{self.serial}/report")
            # P1/A1 only send deltas after this; harmless on X1/H2.
            c.publish(f"device/{self.serial}/request", json.dumps(
                {"pushing": {"sequence_id": "1", "command": "pushall", "version": 1, "push_target": 1}}))
            store.update_printer_state(self.serial, connected=True)

        def on_disconnect(c, userdata, rc, properties=None):
            log.warning("[%s] disconnected (rc=%s)", self.name, rc)
            store.update_printer_state(self.serial, connected=False)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = lambda c, userdata, msg: self._handle_message(msg)

        self._client = client
        client.connect(self.ip, MQTT_PORT, keepalive=30)
        while not self._stop_event.is_set():
            rc = client.loop(timeout=1.0)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"connection lost (mqtt rc={rc})")

    # -- message handling --------------------------------------------------
    def _handle_message(self, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        info = payload.get("print")
        if not isinstance(info, dict):
            return

        changes = {}
        events = []  # (kind, value, old)

        new_state = info.get("gcode_state")
        if new_state and new_state != self.gcode_state:
            old = self.gcode_state
            self.gcode_state = changes["gcode_state"] = new_state
            if old is not None:  # first sighting is a silent baseline
                events.append(("state", new_state, old))

        if "print_error" in info:
            new_error = info.get("print_error") or 0
            if new_error != self.print_error:
                old = self.print_error
                self.print_error = changes["print_error"] = new_error
                if new_error and old is not None:
                    events.append(("error", new_error, old))

        task = info.get("subtask_name")
        if task and task != self.task_name:
            self.task_name = changes["task_name"] = task
        percent = info.get("mc_percent")
        if isinstance(percent, (int, float)) and percent != self.progress:
            self.progress = changes["progress"] = percent

        now = time.time()
        if changes or now - self._last_write >= HEARTBEAT_WRITE_INTERVAL:
            changes["last_seen"] = store.now_iso()
            store.update_printer_state(self.serial, **changes)
            self._last_write = now

        for kind, value, old in events:
            self._raise_alert(kind, value, old)

    def _task_suffix(self) -> str:
        parts = []
        if self.task_name:
            parts.append(f"任务：{self.task_name}")
        if isinstance(self.progress, (int, float)):
            parts.append(f"进度 {int(self.progress)}%")
        return f"（{'，'.join(parts)}）" if parts else ""

    def _raise_alert(self, kind, value, old):
        config = store.load_config()
        if kind == "state":
            meta = STATE_ALERTS.get(value)
            if meta:
                config_key, label, text = meta
                if value == "RUNNING" and old not in START_FROM:
                    # resumed after a pause: log it, never push it
                    store.add_alert(f"{self.name} 继续打印", "暂停后恢复打印。", printer=self.name)
                    return
                title, message, push = f"{self.name} {label}", f"{text}{self._task_suffix()}", config.get(config_key)
            else:
                return
        else:  # error
            desc = store.parse_error_code_map(config.get("error_code_map", "")).get(value)
            title = f"{self.name} 打印异常"
            if desc:
                message = f"错误代码 {value}：{desc}"
            else:
                message = f"错误代码 {value}（0x{value:X}），暂无自定义说明，请检查设备。"
            message += self._task_suffix()
            push = config.get("alert_on_error_code", True)

        log.info("[%s] alert: %s - %s", self.name, title, message)
        store.add_alert(title, message, printer=self.name)
        if push:
            # off-thread so slow endpoints never stall MQTT keepalive
            threading.Thread(target=notify.send_all, args=(config, title, message), daemon=True).start()


class WatcherManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._watchers = []

    def sync(self):
        """(Re)start one watcher per enabled, fully-configured printer."""
        with self._lock:
            for w in self._watchers:
                w.signal_stop()
            for w in self._watchers:
                w.join()
            self._watchers = []
            for p in store.load_config().get("printers", []):
                if p.get("enabled", True) and p.get("ip") and p.get("serial") and p.get("access_code"):
                    watcher = PrinterWatcher(p)
                    watcher.start()
                    self._watchers.append(watcher)
            if not self._watchers:
                log.info("No printers configured yet - waiting for settings")

    def sync_async(self):
        threading.Thread(target=self.sync, daemon=True).start()

    def stop(self):
        with self._lock:
            for w in self._watchers:
                w.signal_stop()


manager = WatcherManager()
