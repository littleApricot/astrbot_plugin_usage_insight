import asyncio
import base64
import io
import json
import zipfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_usage_insight.balances import BalanceMonitor
from astrbot_plugin_usage_insight.ledger import Ledger, prices_from_json
from astrbot_plugin_usage_insight.main import UsageInsight
from astrbot_plugin_usage_insight.pricing import PricingCatalog
from astrbot_plugin_usage_insight.reconciliation import (
    FIELDS,
    Reconciliations,
    compare,
    number,
    preview,
)
from fastapi import FastAPI, Request

KEY = "sk-synthetic-private-key-for-tests-only"
DATE = "2026-10-20"
TS = datetime(2026, 10, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def upload(text, filename="official.csv"):
    return {"filename": filename, "content": base64.b64encode(text.encode()).decode()}


def payload(*, amount=True, key=True):
    result = upload(
        "date,key,model,input,cached,output,amount,currency\n"
        f"{DATE},{KEY},test-model,1000000,200000,100000,2.000000,CNY\n"
        f"{DATE},other-key,test-model,9000000,0,0,99.000000,CNY\n"
        f"2026-10-19,{KEY},test-model,5000000,0,0,88.000000,CNY\n"
    )
    result.update(
        file_index=0,
        mapping={field: i for i, field in enumerate(FIELDS)},
        key_value_index=0,
        file_is_current_key=not key,
        start=DATE,
        end=DATE,
        provider_id="official",
        csv_timezone="Asia/Shanghai",
        input_mode="total",
        currency="CNY",
    )
    if not amount:
        result["mapping"]["amount"] = -1
    if not key:
        result["mapping"]["key"] = -1
    return result


@pytest.fixture
def state(tmp_path):
    provider = SimpleNamespace(
        provider_config={"id": "official", "api_base": "https://api.deepseek.com"},
        get_current_key=lambda: KEY,
    )
    ledger = Ledger(tmp_path / "usage.db")
    ledger.started = TS - 86400
    catalog = PricingCatalog(tmp_path)
    monitor = BalanceMonitor(
        SimpleNamespace(get_all_providers=lambda: [provider]), ledger, {}, catalog
    )
    ref = monitor.credential_ref(provider)
    row = {
        "ts": TS,
        "duration": 1,
        "provider": "official",
        "model": "test-model",
        "kind": "reply",
        "origin": "test-group",
        "actor": None,
        "participants": [],
        "unit": "call",
        "status": "completed",
        "usage": {"input": 1000000, "cached": 200000, "output": 100000},
        "balance_ref": ref,
    }
    rules = prices_from_json(
        [
            {
                "provider": "",
                "model": "test-model",
                "input": 2,
                "cached": 0.5,
                "output": 3,
            }
        ]
    )
    ledger.put(row)
    args = (ledger, ref, ["official"], monitor.salt, rules, catalog, "Asia/Shanghai")
    yield args, row
    ledger.close()


def test_independent_amount_and_usage_match_excludes_other_keys_and_dates(state):
    args, row = state
    args[0].put(
        {
            **row,
            "balance_ref": "another-key",
            "usage": {"input": 9000000, "cached": 0, "output": 0},
        }
    )
    result = compare(payload(), *args)
    assert result["state"] == "matched"
    assert result["key_binding"] == "full_key_matched"
    assert result["other_key_rows"] == 1 and result["outside_rows"] == 1
    assert result["models"][0]["local_records"] == 1
    assert result["models"][0]["amounts"][0]["local"] == "2.0"
    assert all(c["difference"] == 0 for c in result["models"][0]["usage"])
    assert KEY not in json.dumps(result)


def test_wrong_official_fee_detected_even_when_tokens_match(state):
    args, _ = state
    data = payload()
    data.update(
        upload(
            base64.b64decode(data["content"]).decode().replace("2.000000", "1.900000")
        )
    )
    result = compare(data, *args)
    assert result["state"] == "different"
    assert result["models"][0]["amounts"][0]["difference"] == "0.100000"
    assert all(c["state"] == "matched" for c in result["models"][0]["usage"])


def test_no_official_amount_never_claims_fee_verification(state):
    args, _ = state
    result = compare(payload(amount=False), *args)
    assert result["state"] == "matched"
    assert result["amount_available"] is False
    assert result["models"][0]["amounts"] == []


@pytest.mark.parametrize(
    "change", ["unknown_usage", "unknown_cache", "untagged", "boundary"]
)
def test_incomplete_local_data_cannot_pass(state, change):
    args, row = state
    if change == "unknown_usage":
        row["usage"] = None
    elif change == "unknown_cache":
        row["usage"]["cached"] = None
    elif change == "untagged":
        row["balance_ref"] = ""
    else:
        row["duration"] = 86400
    args[0].db.execute("DELETE FROM receipts")
    args[0].put(row)
    result = compare(payload(), *args)
    assert result["state"] == "incomplete"


def test_full_key_mismatch_and_missing_user_binding_rejected(state):
    args, _ = state
    data = payload()
    data.update(
        upload(
            base64.b64decode(data["content"])
            .decode()
            .replace(KEY, "sk-other-completely-different-private-test-key")
        )
    )
    with pytest.raises(ValueError, match="不一致"):
        compare(data, *args)
    data = payload(key=False)
    data["file_is_current_key"] = False
    with pytest.raises(ValueError, match="确认"):
        compare(data, *args)


def test_masked_key_and_input_miss_semantics(state):
    args, _ = state
    data = payload()
    data.update(
        upload(
            base64.b64decode(data["content"])
            .decode()
            .replace(KEY, "main-key ***1234")
            .replace("1000000,200000", "800000,200000")
        )
    )
    data["input_mode"] = "miss"
    result = compare(data, *args)
    assert result["state"] == "matched"
    assert result["key_binding"] == "official_key_value_selected_by_user"


def test_csv_zip_preview_redacts_and_persisted_summary_contains_no_raw_rows(state):
    args, _ = state
    data = payload()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../nested/amount.csv", base64.b64decode(data["content"]))
        archive.writestr("metadata.txt", "ignored")
    data["content"] = base64.b64encode(buffer.getvalue()).decode()
    data["key_column"] = 1
    view = preview(data)
    assert view["files"][0]["name"] == "amount.csv" and len(view["keys"]) == 2
    assert KEY not in json.dumps(view)
    reports = Reconciliations(args[0])
    result = compare(data, *args)
    reports.save(result)
    reports.save(compare(data, *args))
    assert len(reports.recent()) == 1
    stored = str(args[0].db.execute("SELECT data FROM reconciliations").fetchall())
    assert (
        KEY not in stored
        and data["content"] not in stored
        and "other-key" not in stored
    )


@pytest.mark.parametrize(
    "bad", ["NaN", "Infinity", "-1", "1.5", "1e-100000", "1e100000"]
)
def test_invalid_token_numbers_rejected(bad):
    with pytest.raises(ValueError):
        number(bad, integer=True)


def test_bad_mapping_timezone_and_cache_input_rejected(state):
    args, _ = state
    data = payload()
    data["mapping"]["cached"] = data["mapping"]["input"]
    with pytest.raises(ValueError, match="同一列"):
        compare(data, *args)
    data = payload()
    data["csv_timezone"] = "Invalid/Zone"
    with pytest.raises(ValueError, match="日期无效"):
        compare(data, *args)
    data = payload()
    data.update(
        upload(
            base64.b64decode(data["content"])
            .decode()
            .replace("1000000,200000", "100,200000")
        )
    )
    with pytest.raises(ValueError, match="缓存输入不能"):
        compare(data, *args)


def test_timestamp_aliases_and_export_rounding(state):
    args, _ = state
    data = payload()
    data.update(
        upload(
            base64.b64decode(data["content"])
            .decode()
            .replace(DATE, "2026-10-20T04:00:00Z")
            .replace("2.000000", "2.00")
        )
    )
    result = compare(data, *args)
    assert result["state"] == "matched"
    assert result["models"][0]["amounts"][0]["precision"] == "0.01"


def test_before_collection_start_is_incomplete_even_if_totals_agree(state):
    args, _ = state
    args[0].started = TS
    result = compare(payload(), *args)
    assert result["state"] == "incomplete"
    assert any("早于详细采集" in note for note in result["notes"])


def test_zero_export_with_no_local_activity_does_not_claim_validation(state):
    args, _ = state
    args[0].db.execute("DELETE FROM receipts")
    args[0].db.commit()
    data = payload()
    data.update(
        upload(
            f"date,key,model,input,cached,output,amount,currency\n{DATE},{KEY},test-model,0,0,0,0.000000,CNY\n"
        )
    )
    assert compare(data, *args)["state"] == "no_activity"


def test_same_key_external_usage_is_a_difference_and_does_not_change_local_ledger(
    state,
):
    args, _ = state
    data = payload()
    data.update(
        upload(
            base64.b64decode(data["content"])
            .decode()
            .replace("1000000,200000,100000,2.000000", "2000000,400000,200000,4.000000")
        )
    )
    result = compare(data, *args)
    assert result["state"] == "different"
    assert result["models"][0]["usage"][0]["difference"] == -1000000
    assert args[0].db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1


def test_only_amount_column_can_be_checked_without_fabricating_usage(state):
    args, _ = state
    data = payload()
    for field in ("input", "cached", "output", "model"):
        data["mapping"][field] = -1
    result = compare(data, *args)
    assert result["state"] == "matched" and result["models"][0]["model"] == "全部模型"
    assert all(check["state"] == "unverified" for check in result["models"][0]["usage"])


def test_actual_authenticated_api_import_and_reopen_summary(tmp_path):
    routes = {}
    provider = SimpleNamespace(
        provider_config={"id": "official", "api_base": "https://api.deepseek.com"},
        get_current_key=lambda: KEY,
    )
    context = SimpleNamespace(
        register_web_api=lambda path, handler, *rest: routes.update(
            {path.rsplit("/", 1)[1]: handler}
        ),
        get_all_providers=lambda: [provider],
        get_config=lambda: {"timezone": "Asia/Shanghai"},
    )
    with patch(
        "astrbot_plugin_usage_insight.main.StarTools.get_data_dir",
        return_value=tmp_path,
    ):
        plugin = UsageInsight(
            context, AstrBotConfig(str(tmp_path / "config.json"), schema={})
        )
    app = FastAPI()
    plugin.ledger.started = TS - 86400

    @app.api_route("/{name}", methods=["GET", "POST"])
    async def endpoint(name: str, request: Request):
        with bind_request_context(
            PluginRequest(
                request, plugin_name="astrbot_plugin_usage_insight", username="test"
            )
        ):
            return await routes[name]()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/official_preview", json=payload())
            assert response.status_code == 200 and KEY not in response.text
            response = await client.post("/official_reconcile", json=payload())
            assert (
                response.status_code == 200 and response.json()["state"] == "different"
            )
            response = await client.get("/official_reconcile")
            assert len(response.json()["reports"]) == 1
            data = payload()
            data["provider_id"] = "not-official"
            assert (
                await client.post("/official_reconcile", json=data)
            ).status_code == 400
            assert (
                await client.post("/official_preview", json={"content": "BAD"})
            ).status_code == 400

    try:
        asyncio.run(run())
    finally:
        plugin.ledger.close()
