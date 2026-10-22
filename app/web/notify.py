"""
Notification channels. Every sender returns None when its channel is
disabled, "ok" on success, or a short error string. Errors never raise and
never contain secrets, so one broken channel can't affect the others.
"""

import json
import logging

import requests

log = logging.getLogger("bambu-alarm.notify")


def _http(method: str, url: str, **kwargs):
    kwargs.setdefault("timeout", 10)
    return requests.request(method, url, **kwargs)


def _result(resp) -> str:
    if resp.status_code < 300:
        return "ok"
    return f"HTTP {resp.status_code}: {resp.text[:200]}"


def _scrub(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def send_telegram(config: dict, title: str, message: str):
    if not config.get("telegram_enabled"):
        return None
    token = config.get("telegram_bot_token", "").strip()
    chat_id = config.get("telegram_chat_id", "").strip()
    if not token or not chat_id:
        return "缺少 Bot Token 或 Chat ID"
    base = (config.get("telegram_api_base") or "https://api.telegram.org").strip().rstrip("/")
    try:
        resp = _http("POST", f"{base}/bot{token}/sendMessage",
                     json={"chat_id": chat_id, "text": f"{title}\n{message}"})
        return _scrub(_result(resp), token)
    except Exception as exc:
        return _scrub(f"请求失败: {exc}", token)


def send_bark(config: dict, title: str, message: str):
    if not config.get("bark_enabled"):
        return None
    key = config.get("bark_key", "").strip()
    if not key:
        return "缺少 Bark Key"
    server = (config.get("bark_server") or "https://api.day.app").strip().rstrip("/")
    try:
        resp = _http("POST", f"{server}/push",
                     json={"device_key": key, "title": title, "body": message, "level": "timeSensitive"})
        return _scrub(_result(resp), key)
    except Exception as exc:
        return _scrub(f"请求失败: {exc}", key)


def send_ntfy(config: dict, title: str, message: str):
    if not config.get("ntfy_enabled"):
        return None
    topic = config.get("ntfy_topic", "").strip()
    if not topic:
        return "缺少 ntfy 主题（topic）"
    server = (config.get("ntfy_server") or "https://ntfy.sh").strip().rstrip("/")
    token = config.get("ntfy_token", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = _http("POST", server, headers=headers,
                     json={"topic": topic, "title": title, "message": message, "priority": 4})
        return _scrub(_result(resp), token)
    except Exception as exc:
        return _scrub(f"请求失败: {exc}", token)


def send_webhook(hook: dict, title: str, message: str):
    """Send through ONE webhook entry. Returns None if that entry is disabled."""
    if not hook.get("enabled", True):
        return None
    url = (hook.get("url") or "").strip()
    if not url:
        return "缺少 URL"
    method = (hook.get("method") or "POST").upper()

    headers = {}
    headers_raw = (hook.get("headers") or "").strip()
    if headers_raw:
        try:
            headers = json.loads(headers_raw)
        except json.JSONDecodeError:
            return "Headers 不是合法的 JSON"

    template = hook.get("body_template") or '{"text": "{message}"}'

    def esc(text):  # make the value safe inside a JSON string literal
        return json.dumps(text, ensure_ascii=False)[1:-1]

    body = template.replace("{title}", esc(title)).replace("{message}", esc(message))
    try:
        kwargs = {"json": json.loads(body)}
    except json.JSONDecodeError:
        # Not JSON: send the template as a plain-text body, unescaped.
        raw = template.replace("{title}", title).replace("{message}", message)
        kwargs = {"data": raw.encode("utf-8")}
    try:
        resp = _http(method, url, headers=headers, **kwargs)
        return _result(resp)
    except Exception as exc:
        return f"请求失败: {exc}"


CHANNELS = (
    ("Telegram", send_telegram),
    ("Bark", send_bark),
    ("ntfy", send_ntfy),
)


def send_all(config: dict, title: str, message: str) -> dict:
    """Send via every enabled channel and webhook; returns {channel: result}."""
    targets = [(name, lambda sender=sender: sender(config, title, message)) for name, sender in CHANNELS]
    for i, hook in enumerate(config.get("webhooks", []), 1):
        label = f"Webhook：{hook.get('name') or i}"
        targets.append((label, lambda hook=hook: send_webhook(hook, title, message)))

    results = {}
    for name, run in targets:
        result = run()
        if result is None:
            continue
        results[name] = result
        if result != "ok":
            log.error("%s push failed: %s", name, result)
    return results
