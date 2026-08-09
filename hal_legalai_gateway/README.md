# HAL LegalAI Gateway (Phase 2)

Thin standalone gateway that **consolidates the interface, not the implementation**.

Bridge, Storage, Mission Control, and artifact retrieval remain separately
deployed, testable, and replaceable. Phase 2 adds an **authenticated remote MCP
endpoint** with stable namespaced tools that forward to those downstream MCP
servers. No Case-00 generation, archive mutation, B2, or mission execution logic
is copied into the gateway.

## What Phase 2 provides

| Surface | Purpose |
| --- | --- |
| `POST/GET /mcp` | Authenticated Streamable HTTP MCP endpoint (FastMCP) |
| Namespaced tools | Settled `case.*`, `storage.*`, `mission.*` surface with thin forwarding |
| `registry.json` | Machine-readable map of namespaces → downstream service IDs, URL env vars, health paths, and tool bindings |
| `GET /registry` | Serve the registry plus resolved (non-secret) downstream URLs |
| `GET /health` | Gateway liveness + **independent** downstream status; reports exact `RAILWAY_GIT_COMMIT_SHA` and **runtime registered tool names** |
| Request IDs | `X-Request-ID` / `X-Correlation-ID` middleware; every forwarded call returns correlation metadata |
| Config validation | Fail closed on missing auth secrets, invalid timeouts, or non-http(s) URLs |

## Settled tool surface (minimum)

| Gateway tool | Downstream service | Downstream MCP tool |
| --- | --- | --- |
| `case.get_artifact` | artifacts | `get_case_artifact` |
| `storage.archive_feedback` | storage | `archive_case00_attorney_feedback` |
| `storage.archive_review_packet` | storage | `archive_case00_review_packet` |
| `storage.verify_archive` | storage | `list_case00_storage` (closest truthful inventory verification) |
| `mission.submit` | mission_control | `submit_run` |
| `mission.status` | mission_control | `get_run` |

Also exposed: Case-00 lifecycle (`case.submit_case00_q1`, `case.get_case00_q1_run`,
`case.cancel_case00_q1_run`, `case.get_case00_q1_artifacts`, `case.get_artifacts`),
`storage.list_inventory`, and mission helpers (`mission.submit_structured`,
`mission.wait`, `mission.submit_and_wait`, `mission.run_repository_command`).

## Authentication and forwarding

**Inbound (required):** `Authorization: Bearer <GATEWAY_API_KEY>`

Write tools are never exposed unauthenticated. Missing `GATEWAY_API_KEY` fails
startup (fail closed).

**Downstream Bridge / Storage / Artifacts:** GitHub OAuth on the Bridge MCP is
**not compatible** with the gateway API key. After inbound auth succeeds, the
gateway resolves Bridge-facing credentials as:

1. Prefer caller `X-Downstream-Authorization` when present (validated caller
   already holds a Bridge-compatible Bearer token), else
2. Use service-to-service `GATEWAY_BRIDGE_AUTHORIZATION` (required at startup).

Never forward the gateway API key to Bridge.

**Downstream Mission Control:** The MCP connector authenticates to the Mission
Control HTTP API with its own server-side `MISSION_CONTROL_API_KEY`. The gateway
gates `mission.*` with `GATEWAY_API_KEY` only and does not put that Mission
Control key on the public wire.

## Failure isolation and observability

Every forwarded call returns (and logs) at least:

- `request_id` / `correlation_id`
- `downstream_service` / `downstream_tool`
- `duration_ms`
- `failure_stage` (`unconfigured` | `auth` | `connect` | `timeout` | `http` |
  `protocol` | `tool` | `parse` | `internal` | `null` on success)

A failure in one namespace/tool does not cancel or poison other tools. Downstream
health probes remain concurrent and independent (Phase 1 behavior preserved).

## Namespaces → downstreams

| Namespace | Downstream key | Default URL env |
| --- | --- | --- |
| `case` | `bridge` (artifacts for `case.get_artifact*`) | `GATEWAY_BRIDGE_URL` / `GATEWAY_ARTIFACTS_URL` |
| `storage` | `storage` | `GATEWAY_STORAGE_URL` |
| `mission` | `mission_control` | `GATEWAY_MISSION_CONTROL_URL` |

`GATEWAY_MISSION_CONTROL_URL` must point at the Mission Control **MCP**
Streamable HTTP base (`SERVICE_MODE=mcp`), not only the REST API service.

## Configuration (Railway variables)

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `GATEWAY_API_KEY` | **yes** | — | Inbound Bearer for `/mcp` (fail closed) |
| `GATEWAY_BRIDGE_AUTHORIZATION` | **yes** | — | Service-to-service Bearer for Bridge MCP (or rely on per-call `X-Downstream-Authorization`) |
| `PORT` | no | `8080` | Listen port |
| `RAILWAY_GIT_COMMIT_SHA` | no | `unknown` | Exact deployed SHA when Railway injects it |
| `GATEWAY_HEALTH_TIMEOUT_SECONDS` | no | `5` | Per-probe timeout (`0.1`–`30`) |
| `GATEWAY_CONNECT_TIMEOUT_SECONDS` | no | `5` | Downstream MCP connect timeout (`0.1`–`30`) |
| `GATEWAY_READ_TIMEOUT_SECONDS` | no | `30` | Downstream MCP read timeout (`0.1`–`120`, ≥ connect) |
| `GATEWAY_MCP_PATH` | no | `/mcp` | Downstream Streamable HTTP path |
| `GATEWAY_BRIDGE_URL` | no | production bridge URL from registry | Absolute `http(s)` base URL |
| `GATEWAY_STORAGE_URL` | no | same default as bridge | Independently replaceable |
| `GATEWAY_MISSION_CONTROL_URL` | no | production Mission Control URL | Absolute `http(s)` MCP base URL |
| `GATEWAY_ARTIFACTS_URL` | no | same default as bridge | Independently replaceable |

## Local run

```bash
cd hal_legalai_gateway
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
export GATEWAY_API_KEY=dev-gateway-key
export GATEWAY_BRIDGE_AUTHORIZATION='Bearer dev-bridge-token'
PYTHONPATH=. uvicorn hal_legalai_gateway.server:app --host 0.0.0.0 --port 8080
```

Smoke:

```bash
curl -sS localhost:8080/health | python -m json.tool
curl -sS localhost:8080/registry | python -m json.tool
# MCP requires Authorization: Bearer $GATEWAY_API_KEY
```

## Tests

From the repository root:

```bash
python -m unittest tests.test_hal_legalai_gateway -v
```

## Docker

```bash
docker build -t hal-legalai-gateway ./hal_legalai_gateway
docker run --rm -p 8080:8080 \
  -e RAILWAY_GIT_COMMIT_SHA="$(git rev-parse HEAD)" \
  -e GATEWAY_API_KEY=... \
  -e GATEWAY_BRIDGE_AUTHORIZATION=... \
  hal-legalai-gateway
```

## Railway provisioning (manual)

This repository does **not** auto-create a new Railway service for the gateway.
`hal_legalai_gateway/railway.json` + `Dockerfile` are code-only deploy config.

**Manual steps (operator):**

1. In the Railway project that hosts Mission Control / the bridge, **create a new service** (if not already present from Phase 1).
2. Point the service root / watch path at `hal_legalai_gateway` (Dockerfile builder).
3. Set **required** `GATEWAY_API_KEY` and `GATEWAY_BRIDGE_AUTHORIZATION`.
4. Set `GATEWAY_*_URL` overrides as needed; ensure `GATEWAY_MISSION_CONTROL_URL` targets MCP mode.
5. Confirm Railway injects `RAILWAY_GIT_COMMIT_SHA`.
6. Expose public networking; health check path is `/health`.
7. Verify `GET /health` shows `deployed_commit_sha`, `registered_tools` (exact runtime names), and independent `downstream.*` entries.
8. Point ChatGPT / HAL MCP clients at `/mcp` with the gateway Bearer **only after** live verification. Do not retire existing plugins until that cutover is confirmed.

## Non-goals

- No Case-00 Q1 generation, private benchmark access, recipient-policy, or storage archive mutation inside the gateway
- No replacement of Mission Control, bridge, or B2 business logic
- No automatic Railway service creation from this repo alone
- No retirement of existing ChatGPT / HAL plugins in this phase (cutover is operator-gated after live gateway verification)
