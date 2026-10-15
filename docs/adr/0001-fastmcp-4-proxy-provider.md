# ADR 0001: Use fastmcp's ProxyProvider seam (cache disabled) for fastmcp 4.x

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
build the proxy server on the 4.x provider API:

1. `ColabSessionProxy.start_proxy_server` creates a plain `FastMCP` and
   attaches `ProxyProvider(client_factory, cache_ttl=0)` plus
   `ProxyMetadataMiddleware(provider, identity="proxy")`.
2. `cache_ttl=0` disables the provider's component-list cache: Colab's
   toolset changes on browser connect/disconnect and the connectivity
   notification cannot invalidate the provider cache (only a client-driven
   `tools/list` refreshes it), so with the 300s default a `tools/call` in the
   reconnect window could resolve from a stale cache. Regressed by
   `test_tool_call_does_not_serve_stale_backend_after_disconnect`.
3. Imports use the 4.x surfaces: `fastmcp.tools` for `Tool`/`ToolResult`, and
   the 4.x context API (`await set_state`/`get_state`,
   `send_notification(ToolListChangedNotification())`).
4. Pin `fastmcp==4.1.0` (exact pin retained; 4.x ships as two dists, `fastmcp`
   + `fastmcp-slim`, which must be installed together).
5. Validate incoming JSON-RPC through a module-level
   `pydantic.TypeAdapter(types.JSONRPCMessage)` in `websocket_server.py`,
   keeping the malformed-message path (send an `Exception` into the read
   stream) intact.
6. Keep `ColabTransport` and `ColabProxyClient` unchanged; the
   `client_factory` swap-on-connect seam is 4.x-compatible.

Trade-offs accepted:

- The `FastMCPProxy` convenience subclass was dropped (it hardcodes
  `ProxyProvider(client_factory)` with the default 300s TTL and offers no
  `cache_ttl` escape hatch). It is not deprecated in 4.1.0, so this can be
  revisited if fastmcp adds a TTL passthrough.
- The subclass's remote ping forwarding is dropped; the mcp SDK answers ping
  locally, which also keeps health pings working while disconnected.

Rejected alternative: implement our own thin proxy server on FastMCP
primitives (dynamic tool registry + call forwarding). Rejected because the
native provider already covers forwarding, schema-relay behavior, elicitation,
and cancellation; owning that surface adds maintenance for no product gain.

## Consequences

- Upgrades within 4.x should be low-friction; the seam is fastmcp's public
  provider API.
- Every tool call/list hits the live backend (cache disabled) — acceptable
  for a single-user local proxy over one websocket; revisit if latency ever
  shows up in profiles.
- The union-type `JSONRPCMessage` means code must never call model methods on
  it directly; use the `TypeAdapter`.

## Future decision points

- Return to the `FastMCPProxy` subclass if fastmcp deprecates the bare
  provider API (unlikely) or adds a `cache_ttl` passthrough with a
  connectivity-driven invalidation hook.
- Re-enable provider caching if profiling ever shows list/call overhead.
