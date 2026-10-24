"""Isolated API/page fixture with synthetic usage, no model or QQ connection."""

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn
from astrbot.api import AstrBotConfig
from astrbot.api.web import PluginRequest, bind_request_context
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import astrbot_plugin_usage_insight.balances as balance_module
from astrbot_plugin_usage_insight.main import UsageInsight

temporary = tempfile.TemporaryDirectory(prefix="usage-insight-ui-")
root = Path(temporary.name)
routes = {}


def register(path, handler, methods, description):
    routes[path.rsplit("/", 1)[1]] = handler


schema = json.loads(
    (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
)
config = AstrBotConfig(str(root / "config.json"), schema=schema)
context = SimpleNamespace(
    register_web_api=register,
    get_all_providers=lambda: [
        SimpleNamespace(
            provider_config={
                "id": "test-deepseek",
                "api_base": "https://api.deepseek.com/v1",
            },
            get_current_key=lambda: "synthetic-ui-key",
        )
    ],
    get_config=lambda: {"timezone": "Asia/Shanghai"},
)
with patch(
    "astrbot_plugin_usage_insight.main.StarTools.get_data_dir", return_value=root
):
    plugin = UsageInsight(context, config)
plugin.import_status = "complete"
plugin.ledger.started = time.time() - 8 * 86400


async def fake_balance_query(key, proxy=None):
    return {
        "ok": True,
        "platform": "DeepSeek",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": "14.94",
                "granted_balance": "0.00",
                "topped_up_balance": "14.94",
            }
        ],
    }


balance_module.query_deepseek_balance = fake_balance_query
ref = plugin.balances.credential_ref(context.get_all_providers()[0])
for age, amount in ((600, "15.00"), (300, "14.95")):
    ts = time.time() - age
    snapshot = {
        "ok": True,
        "platform": "DeepSeek",
        "checked_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        "is_available": True,
        "balance_infos": [
            {
                "currency": "CNY",
                "total_balance": amount,
                "granted_balance": "0.00",
                "topped_up_balance": amount,
            }
        ],
        "request_start": ts - 1,
        "request_end": ts,
        "inflight": False,
        "collecting": True,
    }
    plugin.ledger.db.execute(
        "INSERT INTO balance_snapshots(credential_ref,ts,data) VALUES(?,?,?)",
        (ref, ts, json.dumps(snapshot)),
    )
plugin.ledger.db.commit()
for i in range(125):
    person = {
        "id": "qq:test-" + str(i % 4),
        "name": ["测试甲", "测试乙", "测试丙", "<script>测试名</script>"][i % 4],
    }
    plugin.ledger.put(
        {
            "ts": time.time() - (i % 7) * 86400,
            "provider": "test-deepseek" if i in (1, 2) else "test-provider",
            "model": "deepseek-flash" if i in (1, 2) else "test-model",
            "kind": "history"
            if i == 2
            else ["reply", "decision", "memory", "recognition"][i % 4],
            "origin": "qq:GroupMessage:synthetic-group",
            "actor": person if i % 4 < 2 else None,
            "participants": [person, {"id": "qq:test-peer", "name": "测试同伴"}]
            if i % 4 < 2
            else [],
            "unit": "turn" if i == 2 else "call",
            "balance_ref": ref if i == 1 else "",
            "status": "completed" if i % 11 else "error",
            "duration": 1.25,
            "usage": {"input": 1000 + i * 10, "cached": 800, "output": 100}
            if i % 11
            else None,
            "error": "",
        }
    )
app = FastAPI()


@app.api_route("/api/{name}", methods=["GET", "POST"])
async def api(name: str, request: Request):
    with bind_request_context(
        PluginRequest(
            request, plugin_name="astrbot_plugin_usage_insight", username="local-test"
        )
    ):
        return await routes[name]()


@app.get("/")
async def page():
    html = (Path(__file__).resolve().parents[1] / "pages/usage/index.html").read_text()
    bridge = """<script>window.AstrBotPluginPage={
      async apiGet(endpoint,params){const r=await fetch('/api/'+endpoint+'?'+new URLSearchParams(params||{}));return await r.json();},
      async apiPost(endpoint,body){const r=await fetch('/api/'+endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}
    };</script>"""
    return HTMLResponse(html.replace("<head>", "<head>" + bridge))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=6198, log_level="warning")
