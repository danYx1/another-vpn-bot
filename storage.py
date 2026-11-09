"""Простое JSON-хранилище пользователей и платежей.

Файлы:
    tg_bot/data/users.json     — подписки пользователей
    tg_bot/data/payments.json  — история платежей в Stars
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
USERS_FILE = DATA_DIR / "users.json"
PAYMENTS_FILE = DATA_DIR / "payments.json"

_lock = threading.Lock()


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, type(default)) else default


def _write(path: Path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _users() -> dict:
    return _read(USERS_FILE, {})


def _payments() -> list:
    return _read(PAYMENTS_FILE, [])


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_left(record: dict) -> int:
    if record.get("unlimited"):
        return 0
    expires = parse_dt(record.get("expires_at"))
    if not expires:
        return 0
    left = expires - datetime.now(timezone.utc)
    return max(0, left.days + (1 if left.seconds or left.microseconds else 0))


def is_unlimited(record: dict) -> bool:
    return bool(record.get("unlimited"))


def get_user(user_id: int) -> dict | None:
    return _users().get(str(user_id))


def extend_subscription(
    user_id: int,
    plan_id: str,
    plan_title: str,
    days: int,
    keys: list[dict],
) -> dict:
    now = datetime.now(timezone.utc)
    with _lock:
        users = _users()
        record = users.get(str(user_id)) or {}
        if days and days > 0:
            expires = parse_dt(record.get("expires_at"))
            base = expires if expires and expires > now else now
            expires_at = (base + timedelta(days=days)).isoformat()
            unlimited = False
        else:
            expires_at = None
            unlimited = True
        record.update(
            {
                "user_id": user_id,
                "plan_id": plan_id,
                "plan_title": plan_title,
                "keys": keys,
                "key": keys[0]["url"] if keys else "",
                "key_title": keys[0].get("name", "VPN") if keys else "",
                "started_at": now.isoformat(),
                "expires_at": expires_at,
                "unlimited": unlimited,
            }
        )
        users[str(user_id)] = record
        _write(USERS_FILE, users)
    return record


def set_keys(user_id: int, keys: list[dict]) -> dict | None:
    with _lock:
        users = _users()
        record = users.get(str(user_id))
        if not record:
            return None
        record["keys"] = keys
        if keys:
            record["key"] = keys[0]["url"]
            record["key_title"] = keys[0].get("name", "VPN")
        users[str(user_id)] = record
        _write(USERS_FILE, users)
    return record


def add_payment(
    user_id: int,
    plan_id: str,
    amount: int,
    currency: str,
    charge_id: str,
    payload: str,
) -> None:
    with _lock:
        payments = _payments()
        payments.append(
            {
                "user_id": user_id,
                "plan_id": plan_id,
                "amount": amount,
                "currency": currency,
                "charge_id": charge_id,
                "payload": payload,
                "created_at": time.time(),
            }
        )
        _write(PAYMENTS_FILE, payments)


def payments_summary() -> dict:
    payments = _payments()
    return {
        "count": len(payments),
        "buyers": len({p.get("user_id") for p in payments if p.get("user_id")}),
        "stars": sum(int(p.get("amount", 0)) for p in payments),
    }
