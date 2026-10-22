import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.common import store
from app.web import notify
from app.web.mqtt_client import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")


@asynccontextmanager
async def lifespan(app):
    manager.sync()
    yield
    manager.stop()


app = FastAPI(title="Bambu Lab Print Alarm", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional HTTP Basic auth, enabled by setting WEB_PASSWORD."""
    if not WEB_PASSWORD:
        return await call_next(request)
    authorized = False
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            authorized = (secrets.compare_digest(user.encode(), WEB_USER.encode())
                          and secrets.compare_digest(password.encode(), WEB_PASSWORD.encode()))
        except Exception:
            authorized = False
    if not authorized:
        return Response("Unauthorized", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Bambu Alarm"'})
    return await call_next(request)


def _status() -> dict:
    config = store.load_config()
    state = store.load_state()
    return {
        "tz": store.APP_TZ_NAME,
        "printers": store.printer_overview(config, state),
        "alerts": state["alerts"],
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"init": _status()})


@app.get("/config", response_class=HTMLResponse)
def config_form(request: Request):
    config = store.load_config()
    return templates.TemplateResponse(request, "config.html", {
        "config": config,
        "saved": request.query_params.get("saved") == "1",
        "auth_enabled": bool(WEB_PASSWORD),
    })


def _flag(form, name: str) -> bool:
    return str(form.get(name, "")).lower() in ("on", "true", "1", "yes")


@app.post("/config")
async def config_save(request: Request):
    form = await request.form()
    old = store.load_config()

    printers, seen = [], set()
    rows = zip(form.getlist("printer_name"), form.getlist("printer_ip"), form.getlist("printer_serial"),
               form.getlist("printer_access_code"), form.getlist("printer_enabled"))
    for name, ip, serial, code, enabled in rows:
        ip, serial, code = ip.strip(), serial.strip(), code.strip()
        if not (ip or serial or code) or serial in seen:
            continue  # skip empty rows and duplicate serial numbers
        seen.add(serial)
        printers.append({"name": name.strip() or "打印机", "ip": ip, "serial": serial,
                         "access_code": code, "enabled": enabled != "0"})

    webhooks = []
    hook_rows = zip(form.getlist("webhook_name"), form.getlist("webhook_enabled"), form.getlist("webhook_method"),
                    form.getlist("webhook_url"), form.getlist("webhook_headers"), form.getlist("webhook_body"))
    for name, enabled, method, url, headers, body in hook_rows:
        if not url.strip():
            continue  # skip empty rows
        webhooks.append({
            "name": name.strip() or f"Webhook {len(webhooks) + 1}",
            "enabled": enabled != "0",
            "method": method.strip().upper() or "POST",
            "url": url.strip(),
            "headers": headers.strip(),
            "body_template": body.strip() or '{"text": "{message}"}',
        })

    def text(name: str, default: str = "") -> str:
        return str(form.get(name, default)).strip()

    new_config = {
        "printers": printers,
        "alert_on_start": _flag(form, "alert_on_start"),
        "alert_on_pause": _flag(form, "alert_on_pause"),
        "alert_on_finish": _flag(form, "alert_on_finish"),
        "alert_on_failed": _flag(form, "alert_on_failed"),
        "alert_on_error_code": _flag(form, "alert_on_error_code"),
        "error_code_map": str(form.get("error_code_map", "")).strip(),
        "telegram_enabled": _flag(form, "telegram_enabled"),
        "telegram_bot_token": text("telegram_bot_token"),
        "telegram_chat_id": text("telegram_chat_id"),
        "telegram_api_base": text("telegram_api_base", "https://api.telegram.org") or "https://api.telegram.org",
        "bark_enabled": _flag(form, "bark_enabled"),
        "bark_server": text("bark_server", "https://api.day.app") or "https://api.day.app",
        "bark_key": text("bark_key"),
        "ntfy_enabled": _flag(form, "ntfy_enabled"),
        "ntfy_server": text("ntfy_server", "https://ntfy.sh") or "https://ntfy.sh",
        "ntfy_topic": text("ntfy_topic"),
        "ntfy_token": text("ntfy_token"),
        "webhooks": webhooks,
    }
    store.save_config(new_config)
    if new_config["printers"] != old.get("printers"):
        manager.sync_async()  # connection settings changed: restart the watchers
    return RedirectResponse(url="/config?saved=1", status_code=303)


@app.get("/api/status")
def api_status():
    return _status()


@app.post("/api/alerts/clear")
def api_clear_alerts():
    store.clear_alerts()
    return {"ok": True}


@app.post("/api/test-notify")
def api_test_notify():
    results = notify.send_all(store.load_config(), "测试推送", "这是一条来自拓竹打印机报警器的测试消息。")
    return {"results": results}
