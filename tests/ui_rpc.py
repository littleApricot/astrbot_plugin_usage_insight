"""Exercise actual API handlers over subprocess pipes, without listening ports."""

import asyncio
import json
import sys

from httpx import ASGITransport, AsyncClient
from ui_server import app


async def run():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            query = json.loads(line)
            try:
                response = await client.request(
                    query["method"], "/api/" + query["path"], json=query.get("body")
                )
                result = response.json()
            except Exception as exc:
                result = {"status": "error", "message": type(exc).__name__}
            print(
                "USAGE_RPC:" + json.dumps({"id": query["id"], "result": result}),
                flush=True,
            )


asyncio.run(run())
