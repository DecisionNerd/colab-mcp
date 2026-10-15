# ADR 0001: Ride fastmcp's proxy provider seam for fastmcp 4.x

Date: 2026-10-15
Status: Accepted

## Context

colab-mcp bridges a local MCP client to a Colab browser session over a single
websocket. The glue lives in `src/colab_mcp/session.py`:

- `ColabTransport` wraps the websocket server's anyio memory streams as a
  fastmcp `ClientTransport`.
- `ColabProxyClient.client_factory` returns either the real backing client
  (when Colab is connected) or a stub client, per call.
- `FastMCPProxy(client_factory=...)` exposes the backing tools/resources and
  forwards calls; middleware watches connectivity and notifies the client via
  `tools/list_changed`.

fastmcp 4.x removed the old `fastmcp.server.proxy` module. The proxy suite
moved to `fastmcp.server.providers.proxy` (`FastMCPProxy`, `ProxyProvider`,
`ProxyClient`), now built on the 4.x provider system rather than the legacy
proxy server. Separately, `mcp` 2.3.0 changed `mcp.types.JSONRPCMessage` from
a pydantic model into a plain union type, breaking direct
`.model_validate_json` usage in the websocket bridge.

## Decision

Keep fastmcp's proxy implementation as the seam (no hand-rolled proxy) and
migrate by surface change only:

1. Import `FastMCPProxy` from `fastmcp.server.providers.proxy` and
   `Tool`/`ToolResult` from `fastmcp.tools` (the `.tools.tool` module is gone).
2. Pin `fastmcp==4.1.0` (exact pin retained; 4.x ships as two dists, `fastmcp`
   + `fastmcp-slim`, which must be installed together).
3. Validate incoming JSON-RPC through a module-level
   `pydantic.TypeAdapter(types.JSONRPCMessage)` in `websocket_server.py`,
   keeping the malformed-message path (send an `Exception` into the read
   stream) intact.
4. Keep `ColabTransport`, `ColabProxyClient`, and the middleware stack
   unchanged; the `client_factory` swap-on-connect seam is 4.x-compatible.

Rejected alternative: implement our own thin proxy server on FastMCP
primitives (dynamic tool registry + call forwarding). Rejected because the
native provider already covers forwarding, schema-relay behavior, elicitation,
and cancellation; owning that surface adds maintenance for no product gain.

## Consequences

- Upgrades within 4.x should be low-friction; the seam is fastmcp's public
  provider API.
- `FastMCPProxy` remains a convenience subclass; if fastmcp deprecates it in
  favor of `FastMCP().add_provider(ProxyProvider(...))`, the migration is one
  construction site in `ColabSessionProxy.start_proxy_server`.
- The 4.x provider caches component lists (default TTL ~300s) and refreshes on
  `list_*`. Colab's toolset changes on browser connect/disconnect; our
  middleware already fires `send_tool_list_changed()` on state change, which
  triggers a re-list. If staleness is ever observed, pass `cache_ttl=0` via
  the provider path.
- The union-type `JSONRPCMessage` means code must never call model methods on
  it directly; use the `TypeAdapter`.

## Future decision points

- Switch `FastMCPProxy` subclass to `add_provider(ProxyProvider(...))` idiom
  when/if fastmcp signals subclass deprecation. Verified 2026-10-15 against
  fastmcp 4.1.0: constructing `FastMCPProxy` emits **no** deprecation warning,
  and the subclass adds behavior a bare `add_provider(ProxyProvider(...))`
  swap would silently drop (`ProxyMetadataMiddleware`, remote ping
  forwarding). Do not swap until fastmcp deprecates the subclass and provides
  equivalents for both.
- Tune or disable provider caching based on observed tool-list freshness.
  Measured 2026-10-15 (`tests/proxy_freshness_test.py`): tool lists reflect
  connect and disconnect immediately through the production assembly
  (mount + middleware), so no caching change is needed today.
