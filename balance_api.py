"""Read account balances from the official DeepSeek API."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx


async def query_deepseek_balance(api_key: str, proxy: str | None = None) -> dict:
    """Query a balance without generating tokens or changing account data.

    Args:
        api_key: Key from the selected chat provider; never returned or logged.
        proxy: Optional proxy already configured for that provider.

    Returns:
        Whitelisted balance fields and a timestamp, or a safe error code.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        return {"ok": False, "error": "missing_api_key"}

    try:
        async with httpx.AsyncClient(
            timeout=15, proxy=proxy, follow_redirects=False
        ) as client:
            response = await client.get(
                "https://api.deepseek.com/user/balance",
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout"}
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "error": "connection_failed"}

    if response.status_code != 200:
        return {
            "ok": False,
            "error": "authentication_failed"
            if response.status_code in (401, 403)
            else "rate_limited"
            if response.status_code == 429
            else "http_error",
            "http_status": response.status_code,
        }

    try:
        data = response.json()
        if not isinstance(data, dict) or type(data.get("is_available")) is not bool:
            raise ValueError("Invalid availability")
        entries = data.get("balance_infos")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
            raise ValueError("Invalid balance list")
        balances = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("currency") not in (
                "CNY",
                "USD",
            ):
                raise ValueError("Invalid currency")
            balance = {"currency": entry["currency"]}
            for field in ("total_balance", "granted_balance", "topped_up_balance"):
                value = entry.get(field)
                if (
                    not isinstance(value, str)
                    or not 1 <= len(value) <= 64
                    or not Decimal(value).is_finite()
                ):
                    raise ValueError("Invalid amount")
                balance[field] = value
            balances.append(balance)
    except (ValueError, InvalidOperation):
        return {"ok": False, "error": "invalid_response"}

    return {
        "ok": True,
        "platform": "DeepSeek",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "is_available": data["is_available"],
        "balance_infos": balances,
    }
