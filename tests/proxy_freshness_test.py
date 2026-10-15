# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json

import pytest
import websockets
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types.version import LATEST_HANDSHAKE_VERSION
from websockets.typing import Origin, Subprotocol

from colab_mcp import session


async def build_prod_server():
    """Assembles the server exactly like colab_mcp.main_async does."""
    proxy = session.ColabSessionProxy()
    await proxy.start_proxy_server()
    assert proxy.proxy_server is not None
    mcp = FastMCP(name="ColabMCP")
    mcp.mount(proxy.proxy_server)
    for middleware in proxy.middleware:
        mcp.add_middleware(middleware)
    return proxy, mcp


async def fake_colab_backend(client):
    """Answers the JSON-RPC methods a fastmcp proxy client sends, advertising one tool."""
    async for raw in client:
        req = json.loads(raw)
        method = req.get("method")
        if method is None or req.get("id") is None:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": LATEST_HANDSHAKE_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-colab", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echoes its input",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            }
        else:
            result = {}
        await client.send(
            json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result})
        )


async def connect_fake_colab(proxy):
    client = await websockets.connect(
        f"ws://localhost:{proxy.wss.port}",
        origin=Origin("https://colab.google.com"),
        subprotocols=[Subprotocol("mcp")],
        additional_headers={"Authorization": f"Bearer {proxy.wss.token}"},
    )
    backend = asyncio.create_task(fake_colab_backend(client))
    proxy_client = proxy.middleware[0].proxy_client
    for _ in range(100):
        if proxy_client.is_connected():
            return client, backend
        await asyncio.sleep(0.05)
    backend.cancel()
    await client.close()
    pytest.fail("proxy never became connected after fake Colab connected")


@pytest.mark.asyncio
async def test_tool_list_refreshes_on_connect():
    """Tools advertised by Colab appear in the proxy tool list once connected."""
    proxy, mcp = await build_prod_server()
    try:
        async with Client(mcp) as mcp_client:
            tools_before = {t.name for t in await mcp_client.list_tools()}
            assert tools_before == {session.INJECTED_TOOL_NAME}

            ws_client, backend = await connect_fake_colab(proxy)
            try:
                tools_after = {t.name for t in await mcp_client.list_tools()}
                assert tools_after == {session.INJECTED_TOOL_NAME, "echo"}
            finally:
                backend.cancel()
                await ws_client.close()
    finally:
        await proxy.cleanup()


@pytest.mark.asyncio
async def test_tool_list_refreshes_on_disconnect():
    """Tools advertised by Colab disappear from the proxy tool list after disconnect."""
    proxy, mcp = await build_prod_server()
    try:
        async with Client(mcp) as mcp_client:
            ws_client, backend = await connect_fake_colab(proxy)
            tools_connected = {t.name for t in await mcp_client.list_tools()}
            assert tools_connected == {session.INJECTED_TOOL_NAME, "echo"}

            backend.cancel()
            await ws_client.close()
            await ws_client.wait_closed()
            proxy_client = proxy.middleware[0].proxy_client
            for _ in range(100):
                if not proxy_client.is_connected():
                    break
                await asyncio.sleep(0.05)
            assert not proxy_client.is_connected()

            tools_after = {t.name for t in await mcp_client.list_tools()}
            assert tools_after == {session.INJECTED_TOOL_NAME}
    finally:
        await proxy.cleanup()


@pytest.mark.asyncio
async def test_tool_call_does_not_serve_stale_backend_after_disconnect():
    """tools/call after disconnect fails instead of hitting a stale cached tool."""
    proxy, mcp = await build_prod_server()
    try:
        async with Client(mcp) as mcp_client:
            ws_client, backend = await connect_fake_colab(proxy)
            result = await mcp_client.call_tool("echo", {})
            assert not result.is_error

            backend.cancel()
            await ws_client.close()
            await ws_client.wait_closed()
            proxy_client = proxy.middleware[0].proxy_client
            for _ in range(100):
                if not proxy_client.is_connected():
                    break
                await asyncio.sleep(0.05)
            assert not proxy_client.is_connected()

            with pytest.raises((ToolError, MCPError)):
                await mcp_client.call_tool("echo", {})
    finally:
        await proxy.cleanup()
