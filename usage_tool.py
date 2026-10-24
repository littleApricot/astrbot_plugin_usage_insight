"""Small, read-only spending summaries for model tools."""

from __future__ import annotations


def brief(bucket):
    records = bucket["records"]
    return {
        "tokens": bucket["total"] if bucket["usage_count"] else None,
        "cost": bucket["cost"],
        "tokens_state": "no_records"
        if not records
        else "reported"
        if bucket["usage_count"] == records
        else "partial"
        if bucket["usage_count"]
        else "unknown",
        "cost_state": "no_records"
        if not records
        else "estimated"
        if bucket["priced_count"] == records
        else "partial"
        if bucket["priced_count"]
        else "unknown",
    }


def person_view(person):
    return {"id": person["id"], "name": person["name"], **brief(person)}


def query_usage(
    ledger, rules, catalog, *, days, person, scope, origin, viewer, limit, timezone
):
    if type(days) not in (int, float) or not 0 <= days <= 36500 or days != int(days):
        raise ValueError("days 应为 0～36500 的整数；1 表示今天，0 表示全部已采集数据")
    if type(limit) not in (int, float) or not 1 <= limit <= 50 or limit != int(limit):
        raise ValueError("limit 应为 1～50 的整数")
    if scope not in ("all", "current_chat"):
        raise ValueError("scope 应为 all 或 current_chat")
    if not isinstance(person, str) or len(person) > 200:
        raise ValueError("person 应为昵称、用户 ID 或 me，最多 200 字符")
    if scope == "current_chat" and not origin:
        raise ValueError("当前事件没有会话标识，无法查询当前群或会话")
    days, limit = int(days), int(limit)
    options = {
        "days": days,
        "timezone": timezone,
        "origin": origin if scope == "current_chat" else "",
        "attribution": "trigger",
        "limit": 1,
        "catalog": catalog,
    }
    report = ledger.report(rules, **options)
    people = [p for p in report["people"] if p["id"] and not p["id"].startswith("__")]
    base = {
        "days": days,
        "timezone": timezone,
        "scope": scope,
        "data_scope": "this_astrbot",
        "attribution": "trigger",
        "money_kind": "estimate",
    }
    query = person.strip()
    if query:
        if query.casefold() in ("me", "self", "myself", "我", "本人", "自己"):
            selected = viewer
        else:
            matches = [p for p in people if p["id"] == query]
            if not matches:
                matches = [p for p in people if p["id"].rsplit(":", 1)[-1] == query]
            if not matches:
                matches = [
                    p for p in people if p["name"].casefold() == query.casefold()
                ]
            if not matches:
                matches = [
                    p for p in people if query.casefold() in p["name"].casefold()
                ]
            if not matches:
                return {
                    **base,
                    "ok": False,
                    "error": "person_not_found",
                    "message": "所选范围未找到该人的采集记录，不表示此人从未产生花费",
                }
            if len(matches) > 1:
                return {
                    **base,
                    "ok": False,
                    "error": "ambiguous_person",
                    "message": "存在多个匹配用户，请根据用户 ID 确认后重新查询",
                    "candidates": [
                        {"id": p["id"], "name": p["name"]} for p in matches[:20]
                    ],
                    "candidates_total": len(matches),
                }
            selected = matches[0]
        report = ledger.report(rules, actor=selected["id"], **options)
        return {
            **base,
            "ok": True,
            "person": {"id": selected["id"], "name": selected["name"]},
            "summary": brief(report["summary"]),
            "note": "仅含此人触发的已采集开销；没有记录或未知费用不能解释为实际零花费",
        }

    rankings = {}
    for currency in sorted({currency for p in people for currency in p["cost"]}):
        ordered = sorted(
            (p for p in people if currency in p["cost"]),
            key=lambda p: (-p["cost"][currency], p["id"]),
        )
        entries, rank, previous = [], 0, None
        for index, p in enumerate(ordered[:limit]):
            amount = p["cost"][currency]
            if index == 0 or amount != previous:
                rank = index + 1
            entries.append(
                {
                    "rank": rank,
                    "id": p["id"],
                    "name": p["name"],
                    "tokens": p["total"],
                    "amount": amount,
                    "cost_state": brief(p)["cost_state"],
                    "tokens_state": brief(p)["tokens_state"],
                }
            )
            previous = amount
        rankings[currency] = {"people_total": len(ordered), "entries": entries}
    shared = [p for p in report["people"] if not p["id"] or p["id"].startswith("__")]
    unpriced = [p for p in people if not p["cost"]]
    return {
        **base,
        "ok": True,
        "summary": brief(report["summary"]),
        "rankings": rankings,
        "people_without_priced_cost": [person_view(p) for p in unpriced[:limit]],
        "people_without_priced_cost_total": len(unpriced),
        "unattributed": [person_view(p) for p in shared],
        "other_app_usage": {
            "state": "unknown",
            "tokens": None,
            "cost": None,
        },
        "note": "排行按已估算花费降序、按币种分别列出；费用不完整时只能代表已估算部分。费用按触发者归属，包含整轮上下文开销；公共及历史未归属部分单列，不计入个人排行。",
    }
