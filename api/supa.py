"""
Thin wrapper around Supabase's auto-generated REST API (PostgREST).
No supabase-py SDK — plain `requests` calls, kept deliberately lean so the
serverless function cold-starts fast.
"""
import os
import requests
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # service_role key — server-side only, bypasses RLS

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
TIMEOUT = 8


def _url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}"


def add_transaction(tx_type: str, amount: float, category: str, note: str | None) -> int:
    payload = {"type": tx_type, "amount": amount, "category": category, "note": note}
    r = requests.post(
        _url("transactions"),
        headers={**HEADERS, "Prefer": "return=representation"},
        json=payload,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()[0]["id"]


def _all_transactions(order="id.desc", limit=None, since=None):
    params = {"select": "*", "order": order}
    if limit:
        params["limit"] = limit
    if since:
        params["created_at"] = f"gte.{since}"
    r = requests.get(_url("transactions"), headers=HEADERS, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_history(limit: int = 10):
    return _all_transactions(limit=limit)


def delete_transaction(tx_id: int) -> bool:
    r = requests.delete(
        _url(f"transactions?id=eq.{tx_id}"),
        headers={**HEADERS, "Prefer": "return=representation"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return len(r.json()) > 0


def delete_last_transaction():
    rows = _all_transactions(limit=1)
    if not rows:
        return None
    delete_transaction(rows[0]["id"])
    return rows[0]


def get_starting_balance() -> float:
    r = requests.get(
        _url("settings"),
        headers=HEADERS,
        params={"select": "value", "key": "eq.starting_balance"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    return float(rows[0]["value"]) if rows else 0.0


def set_starting_balance(amount: float):
    r = requests.post(
        _url("settings"),
        headers={**HEADERS, "Prefer": "resolution=merge-duplicates"},
        json={"key": "starting_balance", "value": str(amount)},
        timeout=TIMEOUT,
    )
    r.raise_for_status()


def get_balance() -> float:
    rows = _all_transactions()
    income = sum(r["amount"] for r in rows if r["type"] == "income")
    expense = sum(r["amount"] for r in rows if r["type"] == "expense")
    return get_starting_balance() + income - expense


def _since(period: str):
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        return None
    return start.isoformat()


def get_stats(period: str = "month"):
    since = _since(period)
    rows = _all_transactions(since=since) if since else _all_transactions()
    total_income = sum(r["amount"] for r in rows if r["type"] == "income")
    total_expense = sum(r["amount"] for r in rows if r["type"] == "expense")
    by_cat: dict[str, dict] = {}
    for r in rows:
        if r["type"] == "expense":
            c = r["category"]
            by_cat.setdefault(c, {"total": 0.0, "cnt": 0})
            by_cat[c]["total"] += r["amount"]
            by_cat[c]["cnt"] += 1
    by_category = sorted(
        ({"category": k, **v} for k, v in by_cat.items()), key=lambda x: -x["total"]
    )
    return {
        "total_income": total_income,
        "total_expense": total_expense,
        "net": total_income - total_expense,
        "by_category": by_category,
    }


def all_data_dump():
    """Full raw dump for backups."""
    return {
        "transactions": _all_transactions(order="id.asc"),
        "starting_balance": get_starting_balance(),
    }
