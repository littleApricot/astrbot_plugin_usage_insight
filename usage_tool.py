"""Minimal CNY totals and full member rankings for the model tool."""

from __future__ import annotations


EXCLUDED_SOURCES = frozenset(
    {("codex-cli", "recognition"), ("bailian-decision", "decision")}
)


def totals(bucket):
    if not bucket["records"]:
        return {"tokens": 0, "cost": 0}
    result = {
        "tokens": bucket["total"] if bucket["usage_count"] else None,
        "cost": bucket["cost"].get("CNY"),
    }
    incomplete = [
        field
        for field, count in (
            ("tokens", bucket["usage_count"]),
            ("cost", bucket["cny_priced_count"]),
        )
        if count < bucket["records"]
    ]
    if incomplete:
        result["incomplete"] = incomplete
    if bucket["failed_without_usage"]:
        result["failed_without_usage"] = bucket["failed_without_usage"]
    return result


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
        excluded_sources=EXCLUDED_SOURCES,
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
        "total_tokens": summary.pop("tokens"),
        "total_cost": summary.pop("cost"),
        **summary,
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
