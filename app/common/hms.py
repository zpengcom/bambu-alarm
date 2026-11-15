"""
Official Bambu Lab error-code descriptions.

The print_error codes a printer reports (e.g. 50364419 = 0x03008003) are looked up in
the official HMS table that Bambu Studio itself uses. The table is picked by the first
three characters of the serial number (094 = H2D) and fetched in the background, cached
under /data and refreshed once a day. If the network is down the cached copy is used
(or nothing), so alerts are never held up for long.
"""

import logging
import os
import re
import threading
import time

import requests

from app.common import store

log = logging.getLogger("bambu-alarm.hms")

# Tried in order: 1) Bambu's own query service  2) the copy bundled in Bambu Studio's official repo
SOURCES = (
    "https://e.bambulab.com/query.php?lang={lang}&d={dev}",
    "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/hms/hms_{lang}_{dev}.json",
)
LANGS = ("zh-cn", "en")
DEFAULT_LANG = "zh-cn"
TTL_SECONDS = 24 * 3600     # re-download the table at most once a day
RETRY_SECONDS = 600         # after a failed download, wait before trying again
HTTP_TIMEOUT = 8

_lock = threading.Lock()
_mem = {}        # (dev, lang) -> {"fetched_at": float, "codes": {ECODE: text}}
_last_try = {}   # (dev, lang) -> time of the last download attempt
_busy = set()    # downloads in progress


def _dev(serial: str) -> str:
    dev = (serial or "").strip()[:3].upper()
    return dev if re.fullmatch(r"[0-9A-Z]{3}", dev) else ""


def _norm_lang(lang: str) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def _cache_path(dev: str, lang: str) -> str:
    return os.path.join(store.DATA_DIR, f"hms_{dev}_{lang}.json")


def _parse(payload: dict, lang: str) -> dict:
    section = ((payload.get("data") or {}).get("device_error")) or {}
    rows = section.get(lang)
    if not isinstance(rows, list):
        rows = next((v for v in section.values() if isinstance(v, list)), [])
    codes = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ecode = str(row.get("ecode", "")).strip().upper()
        text = " ".join(str(row.get("intro", "")).split())
        if ecode and text:
            codes[ecode] = text
    return codes


def _download(dev: str, lang: str):
    for template in SOURCES:
        url = template.format(dev=dev, lang=lang)
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "bambu-alarm"})
            resp.raise_for_status()
            codes = _parse(resp.json(), lang)
            if codes:
                return codes
            log.warning("HMS table from %s had no error codes", url)
        except Exception as exc:
            log.warning("HMS fetch failed (%s): %s", url, exc)
    return None


def _load(dev: str, lang: str):
    key = (dev, lang)
    with _lock:
        entry = _mem.get(key)
    if entry is None:
        data = store._read_json(_cache_path(dev, lang))
        if isinstance(data.get("codes"), dict) and data["codes"]:
            entry = {"fetched_at": float(data.get("fetched_at") or 0), "codes": data["codes"]}
            with _lock:
                _mem[key] = entry
    return entry


def _refresh(dev: str, lang: str):
    key = (dev, lang)
    with _lock:
        if key in _busy:
            return
        _busy.add(key)
        _last_try[key] = time.time()
    try:
        codes = _download(dev, lang)
        if not codes:
            return
        entry = {"fetched_at": time.time(), "codes": codes}
        with _lock:
            _mem[key] = entry
        try:
            store._atomic_write(_cache_path(dev, lang), entry)
        except OSError as exc:
            log.warning("could not write HMS cache: %s", exc)
        log.info("official error-code table loaded: dev=%s lang=%s (%d codes)", dev, lang, len(codes))
    finally:
        with _lock:
            _busy.discard(key)


def _due(key, entry) -> bool:
    now = time.time()
    stale = entry is None or now - entry["fetched_at"] > TTL_SECONDS
    return stale and now - _last_try.get(key, 0) > RETRY_SECONDS


def prefetch(serial: str, lang: str = DEFAULT_LANG):
    """Warm the cache in the background (no-op when it is fresh)."""
    dev, lang = _dev(serial), _norm_lang(lang)
    if not dev:
        return
    if _due((dev, lang), _load(dev, lang)):
        threading.Thread(target=_refresh, args=(dev, lang), daemon=True, name=f"hms-{dev}").start()


def lookup(code, serial: str, lang: str = DEFAULT_LANG, fetch: bool = True) -> str:
    """Official description for a print_error code ('' if unknown).

    fetch=True may wait a few seconds, but only when no table has ever been
    downloaded; fetch=False never blocks (it starts a background download instead).
    """
    dev, lang = _dev(serial), _norm_lang(lang)
    if not code or not dev:
        return ""
    key = (dev, lang)
    entry = _load(dev, lang)
    if entry is None and fetch and _due(key, None):
        _refresh(dev, lang)
        entry = _load(dev, lang)
    else:
        prefetch(serial, lang)
    if entry is None:
        return ""
    return entry["codes"].get(f"{int(code):08X}", "")
