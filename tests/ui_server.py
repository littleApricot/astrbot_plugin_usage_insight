"""Isolated API/page fixture with synthetic usage, no model or QQ connection."""

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn
from astrbot.api import AstrBotConfig
from astrbot.api.web import PluginRequest, bind_request_context
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
    get_all_providers=lambda: [],
    get_config=lambda: {"timezone": "Asia/Shanghai"},
)
with patch(
    "astrbot_plugin_usage_insight.main.StarTools.get_data_dir", return_value=root
):
    plugin = UsageInsight(context, config)
plugin.import_status = "complete"
for i in range(125):
    person = {
        "id": "qq:test-" + str(i % 4),
        "name": ["测试甲", "测试乙", "测试丙", "<script>测试名</script>"][i % 4],
    }
    plugin.ledger.put(
        {
            "ts": time.time() - (i % 7) * 86400,
            "provider": "test-provider",
            "model": "test-model",
            "kind": ["reply", "decision", "memory", "recognition"][i % 4],
            "origin": "qq:GroupMessage:synthetic-group",
            "actor": person if i % 4 < 2 else None,
            "participants": [person, {"id": "qq:test-peer", "name": "测试同伴"}]
            if i % 4 < 2
            else [],
            "unit": "call",
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
