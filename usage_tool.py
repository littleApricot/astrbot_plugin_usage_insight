"""Minimal CNY totals and full member rankings for the model tool."""

from __future__ import annotations


def totals(bucket):
    if not bucket["records"]:
        return {"tokens": 0, "cost": 0}
    return {
        "tokens": bucket["total"]
        if bucket["usage_count"] == bucket["records"]
        else None,
        "cost": bucket["cost"].get("CNY")
        if bucket["priced_count"] == bucket["records"]
        and set(bucket["cost"]) == {"CNY"}
        else None,
    }


def query_usage(ledger, rules, catalog, *, days, timezone):
    if type(days) not in (int, float) or not 0 <= days <= 36500 or days != int(days):
        raise ValueError("days 应为 0～36500 的整数；1 表示今天，0 表示全部已采集数据")
    report = ledger.report(
        rules,
        days=int(days),
        timezone=timezone,
        attribution="trigger",
        limit=1,
        catalog=catalog,
    )
    people = [
        {"name": person["name"], **totals(person), "_id": person["id"]}
        for person in report["people"]
        if person["id"] and not person["id"].startswith("__")
    ]
    people.sort(
        key=lambda person: (
            person["cost"] is None,
            -(person["cost"] or 0),
            person["tokens"] is None,
            -(person["tokens"] or 0),
            person["_id"],
        )
    )
    summary = totals(report["summary"])
    result = {
        "total_tokens": summary["tokens"],
        "total_cost": summary["cost"],
        "people": [
            {key: value for key, value in person.items() if key != "_id"}
            for person in people
        ],
    }
    compression = next(
        (kind for kind in report["kinds"] if kind["id"] == "compression"), None
    )
    if compression is not None:
        result["compression"] = totals(compression)
    return result
