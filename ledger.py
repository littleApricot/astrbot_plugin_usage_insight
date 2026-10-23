"""Local append-only usage receipts and explicitly labelled price estimates."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

KINDS = {
    "reply": "正式回复",
    "decision": "回复判断",
    "memory": "记忆整理",
    "recognition": "图片识别",
    "other": "其他调用",
    "history": "宿主历史",
}


def prices_from_json(value):
    rules = json.loads(value) if isinstance(value, str) else value
    if not isinstance(rules, list) or len(rules) > 200:
        raise ValueError("价格规则必须是数组，最多 200 条")
    result, keys = [], set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("每条价格规则必须是对象")
        if set(rule) - {"provider", "model", "currency", "input", "cached", "output"}:
            raise ValueError("价格规则含有不支持的字段")
        model, provider = (
            str(rule.get("model", "")).strip(),
            str(rule.get("provider", "")).strip(),
        )
        currency = str(rule.get("currency", "CNY")).strip().upper()
        if (
            not model
            or len(model) > 200
            or len(provider) > 200
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
        ):
            raise ValueError("请填写模型名和三位货币代码，例如 CNY 或 USD")
        if (provider, model) in keys:
            raise ValueError("同一提供商和模型不能重复定价")
        keys.add((provider, model))
        item = {"provider": provider, "model": model, "currency": currency}
        for field in ("input", "cached", "output"):
            try:
                if isinstance(rule.get(field), bool):
                    raise ValueError()
                number = Decimal(str(rule[field]))
                if not number.is_finite() or not 0 <= number <= Decimal("1000000000"):
                    raise ValueError()
            except (KeyError, InvalidOperation, ValueError):
                raise ValueError(
                    "输入、缓存和输出价格必须是非负数，单位为每百万 token"
                ) from None
            item[field] = str(number)
        result.append(item)
    return result


def estimate(row, rules):
    usage = row.get("usage")
    if usage is None:
        return None
    rule = next(
        (
            r
            for r in rules
            if r["provider"] == row["provider"] and r["model"] == row["model"]
        ),
        None,
    )
    rule = rule or next(
        (r for r in rules if not r["provider"] and r["model"] == row["model"]), None
    )
    if rule is None:
        return None
    cached = usage["cached"]
    if cached is None and Decimal(rule["input"]) != Decimal(rule["cached"]):
        return None
    hit = Decimal(cached or 0)
    miss = Decimal(usage["input"]) - hit
    million = Decimal(1000000)
    input_cost = miss * Decimal(rule["input"]) / million
    cache_cost = hit * Decimal(rule["cached"]) / million
    output_cost = Decimal(usage["output"]) * Decimal(rule["output"]) / million
    return {
        "currency": rule["currency"],
        "total": float(input_cost + cache_cost + output_cost),
        "input": float(input_cost),
        "cached": float(cache_cost),
        "output": float(output_cost),
        "saving": float(
            hit * (Decimal(rule["input"]) - Decimal(rule["cached"])) / million
        ),
    }


def empty_bucket(name="", identifier=""):
    return {
        "id": identifier,
        "name": name,
        "calls": 0,
        "turns": 0,
        "errors": 0,
        "input": 0,
        "output": 0,
        "cached": 0,
        "cache_input": 0,
        "usage_count": 0,
        "priced_count": 0,
        "records": 0,
        "duration_total": 0,
        "cost": {},
        "saving": {},
        "cost_parts": {},
    }


def add(bucket, row, cost, weight=1):
    bucket["records"] += weight
    bucket["calls" if row["unit"] == "call" else "turns"] += weight
    bucket["errors"] += weight * (row["status"] not in ("completed", "success"))
    bucket["duration_total"] += row["duration"] * weight
    usage = row["usage"]
    if usage is not None:
        bucket["usage_count"] += weight
        bucket["input"] += usage["input"] * weight
        bucket["output"] += usage["output"] * weight
        if usage["cached"] is not None:
            bucket["cached"] += usage["cached"] * weight
            bucket["cache_input"] += usage["input"] * weight
    if cost is not None:
        bucket["priced_count"] += weight
        currency = cost["currency"]
        bucket["cost"][currency] = (
            bucket["cost"].get(currency, 0) + cost["total"] * weight
        )
        bucket["saving"][currency] = (
            bucket["saving"].get(currency, 0) + cost["saving"] * weight
        )
        parts = bucket["cost_parts"].setdefault(
            currency, {"input": 0, "cached": 0, "output": 0}
        )
        for key in parts:
            parts[key] += cost[key] * weight


def finish(bucket):
    total = bucket["input"] + bucket["output"]
    bucket.update(
        total=total,
        cache_rate=bucket["cached"] / bucket["cache_input"]
        if bucket["cache_input"]
        else None,
        cache_coverage=bucket["cache_input"] / bucket["input"]
        if bucket["input"]
        else None,
        input_ratio=bucket["input"] / total if total else None,
        output_ratio=bucket["output"] / total if total else None,
        average_seconds=bucket["duration_total"] / bucket["records"]
        if bucket["records"]
        else None,
    )
    return bucket


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, ts REAL NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
                origin TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS receipts_ts ON receipts(ts);
            CREATE INDEX IF NOT EXISTS receipts_provider ON receipts(provider,model,ts);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES('started', ?)", (str(time.time()),)
        )
        self.db.commit()
        self.started = float(
            self.db.execute("SELECT value FROM meta WHERE key='started'").fetchone()[0]
        )

    def put(self, row, identifier=None, *, commit=True):
        actor = row.get("actor") or {}
        self.db.execute(
            "INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier or uuid.uuid4().hex,
                row["ts"],
                row["provider"],
                row["model"],
                row["kind"],
                row["origin"],
                actor.get("id", ""),
                json.dumps(row, ensure_ascii=False, allow_nan=False),
            ),
        )
        if commit:
            self.db.commit()

    def read_rows(self, query, params):
        connection = sqlite3.connect("file:" + self.path + "?mode=ro", uri=True)
        try:
            yield from connection.execute(query, params)
        finally:
            connection.close()

    def report(
        self,
        rules,
        *,
        days=7,
        timezone="Asia/Shanghai",
        provider="",
        model="",
        kind="",
        origin="",
        actor="",
        attribution="trigger",
        offset=0,
        limit=50,
    ):
        if attribution not in ("trigger", "participants"):
            raise ValueError("归因方式无效")
        if (
            not 0 <= days <= 36500
            or not 0 <= offset <= 10000000
            or not 1 <= limit <= 100
        ):
            raise ValueError("查询范围无效")
        tz = ZoneInfo(timezone)
        now = datetime.now(tz)
        since = (
            (
                now.replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=max(0, days - 1))
            ).timestamp()
            if days
            else 0
        )
        clauses, params = ["ts>=?"], [since]
        for key, value in (
            ("provider", provider),
            ("model", model),
            ("kind", kind),
            ("origin", origin),
        ):
            if value:
                clauses.append(key + "=?")
                params.append(value)
        total, groups = (
            empty_bucket(),
            {k: {} for k in ("people", "models", "kinds", "origins", "trend")},
        )
        recent, count, models = [], 0, set()
        for (data,) in self.read_rows(
            "SELECT data FROM receipts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts DESC,id DESC",
            params,
        ):
            row = json.loads(data)
            participants = row.get("participants") or (
                [row["actor"]] if row.get("actor") else []
            )
            persons = (
                participants
                if attribution == "participants"
                else ([row["actor"]] if row.get("actor") else [])
            )
            if actor == "__shared__":
                if persons:
                    continue
            elif actor and not any(p["id"] == actor for p in persons):
                continue
            cost = estimate(row, rules)
            # Filtering one participant returns their attributed share, not the full batch.
            weight = 1 / len(persons) if actor and persons else 1
            add(total, row, cost, weight)
            day = datetime.fromtimestamp(row["ts"], tz).strftime("%Y-%m-%d")
            model_id = row["provider"] + "/" + row["model"]
            models.add((row["provider"], row["model"]))
            dimensions = {
                "models": (model_id, model_id),
                "kinds": (row["kind"], KINDS.get(row["kind"], row["kind"])),
                "origins": (row["origin"], row["origin"] or "未知来源"),
                "trend": (day, day),
            }
            for key, (identifier, label) in dimensions.items():
                add(
                    groups[key].setdefault(identifier, empty_bucket(label, identifier)),
                    row,
                    cost,
                    weight,
                )
            if persons:
                for person in persons:
                    if not actor or actor == person["id"]:
                        add(
                            groups["people"].setdefault(
                                person["id"], empty_bucket(person["name"], person["id"])
                            ),
                            row,
                            cost,
                            1 / len(persons),
                        )
            else:
                add(
                    groups["people"].setdefault(
                        "", empty_bucket("共享 / 无法归属", "")
                    ),
                    row,
                    cost,
                )
            if offset <= count < offset + limit:
                recent.append({**row, "cost": cost, "attribution_weight": weight})
            count += 1
        return {
            "summary": finish(total),
            **{
                key: sorted(
                    (finish(v) for v in group.values()),
                    key=lambda v: v["total"],
                    reverse=True,
                )
                for key, group in groups.items()
            },
            "recent": recent,
            "count": count,
            "offset": offset,
            "limit": limit,
            "models_seen": [{"provider": p, "model": m} for p, m in sorted(models)],
            "started": self.started,
            "timezone": timezone,
            "attribution": attribution,
            "prices": rules,
        }

    def close(self):
        self.db.close()
