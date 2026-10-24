"""Shared account-balance snapshots for the model tool and dashboard."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .balance_api import query_deepseek_balance
from .ledger import add, empty_bucket, estimate, finish


def supported(provider):
    try:
        url = urlsplit(str(provider.provider_config.get("api_base") or ""))
        return (
            url.scheme == "https"
            and url.hostname == "api.deepseek.com"
            and url.port in (None, 443)
            and url.username is None
            and url.password is None
        )
    except Exception:
        return False


class BalanceMonitor:
    def __init__(self, context, ledger, config, pricing):
        self.context, self.ledger, self.config = context, ledger, config
        self.pricing = pricing
        ledger.db.executescript("""
            CREATE TABLE IF NOT EXISTS balance_snapshots(
                id INTEGER PRIMARY KEY, credential_ref TEXT NOT NULL,
                ts REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS balance_ref_ts ON balance_snapshots(credential_ref,ts);
        """)
        ledger.db.execute(
            "INSERT OR IGNORE INTO meta VALUES('balance_salt',?)",
            (secrets.token_hex(32),),
        )
        ledger.db.commit()
        self.salt = bytes.fromhex(
            ledger.db.execute(
                "SELECT value FROM meta WHERE key='balance_salt'"
            ).fetchone()[0]
        )
        self.cache, self.last_errors, self.locks = {}, {}, {}

    def credential_ref(self, provider):
        if not supported(provider):
            return ""
        try:
            key = provider.get_current_key()
            if not isinstance(key, str) or not key.strip():
                return ""
            return hmac.new(self.salt, key.strip().encode(), hashlib.sha256).hexdigest()
        except Exception:
            return ""

    def targets(self):
        groups = {}
        for provider in self.context.get_all_providers():
            ref = self.credential_ref(provider)
            if ref:
                group = groups.setdefault(
                    ref, {"provider": provider, "provider_ids": []}
                )
                group["provider_ids"].append(
                    str(provider.provider_config.get("id", ""))
                )
        return groups

    async def query(self, provider, *, force=False):
        if provider is None:
            return {"ok": False, "error": "provider_not_found"}
        if not supported(provider):
            return {"ok": False, "error": "unsupported_platform"}
        ref = self.credential_ref(provider)
        if not ref:
            return {"ok": False, "error": "missing_api_key"}
        provider_id = str(provider.provider_config.get("id", ""))
        async with self.locks.setdefault(ref, asyncio.Lock()):
            cached = self.cache.get(ref)
            if cached and time.monotonic() - cached[0] < (2 if force else 15):
                return {
                    **copy.deepcopy(cached[1]),
                    "provider_id": provider_id,
                    "cached": True,
                }
            try:
                # Keep the selected key local to this request; persist only an HMAC reference.
                key = provider.get_current_key()
                if (
                    hmac.new(
                        self.salt, key.strip().encode(), hashlib.sha256
                    ).hexdigest()
                    != ref
                ):
                    return {"ok": False, "error": "provider_key_changed"}
                result = await query_deepseek_balance(
                    key, provider.provider_config.get("proxy") or None
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {"ok": False, "error": "provider_configuration_error"}
            result = {**result, "provider_id": provider_id, "cached": False}
            if result.get("ok"):
                snapshot = {
                    **result,
                    "recorded_at": time.time(),
                }
                try:
                    self.ledger.db.execute(
                        "INSERT INTO balance_snapshots(credential_ref,ts,data) VALUES(?,?,?)",
                        (
                            ref,
                            snapshot["recorded_at"],
                            json.dumps(snapshot, ensure_ascii=False),
                        ),
                    )
                    self.ledger.db.commit()
                    self.last_errors.pop(ref, None)
                except Exception:
                    # Return a valid balance even if saving its snapshot fails.
                    result["snapshot_saved"] = False
                    self.last_errors[ref] = {
                        "error": "snapshot_save_failed",
                        "attempted_at": time.time(),
                    }
                self.cache[ref] = (time.monotonic(), result)
            else:
                self.last_errors[ref] = {
                    "error": result.get("error", "query_failed"),
                    "attempted_at": time.time(),
                }
                self.cache[ref] = (time.monotonic(), result)
            return copy.deepcopy(result)

    async def refresh(self):
        results = []
        for group in self.targets().values():
            results.append(await self.query(group["provider"], force=True))
        return results

    async def run(self):
        while True:
            if self.config.get("balance_auto_refresh", True):
                await self.refresh()
            await asyncio.sleep(
                max(30, min(86400, int(self.config.get("balance_interval", 300))))
            )

    async def view(self, rules, *, days=7, timezone="Asia/Shanghai"):
        targets = {
            ref: sorted(set(g["provider_ids"])) for ref, g in self.targets().items()
        }
        return await asyncio.to_thread(
            self.report,
            targets,
            rules,
            copy.deepcopy(self.last_errors),
            days=days,
            timezone=timezone,
        )

    def report(self, targets, rules, errors=None, *, days=7, timezone="Asia/Shanghai"):
        if not 0 <= days <= 36500:
            raise ValueError("查询范围无效")
        now = datetime.now(ZoneInfo(timezone))
        since = (
            (
                now.replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=max(0, days - 1))
            ).timestamp()
            if days
            else 0
        )
        groups = []
        for ref, providers in targets.items():
            snapshots = [
                json.loads(data)
                for (data,) in self.ledger.read_rows(
                    "SELECT data FROM balance_snapshots WHERE credential_ref=? ORDER BY ts DESC,id DESC LIMIT 1",
                    (ref,),
                )
            ]
            usage, untagged = empty_bucket(), 0
            for (data,) in self.ledger.read_rows(
                "SELECT data FROM receipts WHERE ts>=?", (since,)
            ):
                row = json.loads(data)
                if row.get("balance_ref") == ref:
                    add(usage, row, estimate(row, rules, self.pricing))
                elif not row.get("balance_ref") and row["provider"] in providers:
                    untagged += 1
            groups.append(
                {
                    "provider_ids": providers,
                    "latest": snapshots[0] if snapshots else None,
                    "last_error": (errors or {}).get(ref),
                    "local_usage": finish(usage),
                    "untagged_records": untagged,
                }
            )
        return {
            "groups": groups,
            "auto_refresh": bool(self.config.get("balance_auto_refresh", True)),
            "interval_seconds": max(
                30, min(86400, int(self.config.get("balance_interval", 300)))
            ),
            "balance_scope": "account",
            "usage_scope": "current_key_this_astrbot",
            "days": days,
        }
