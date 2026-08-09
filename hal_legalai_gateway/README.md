# HAL LegalAI Gateway (Phase 1)

Thin standalone gateway that **consolidates the interface, not the implementation**.

Bridge, Storage, Mission Control, and artifact retrieval remain separately
deployed, testable, and replaceable. Phase 1 does **not** re-implement
downstream business logic.

## What Phase 1 provides

| Surface | Purpose |
| --- | --- |
| `registry.json` | Machine-readable map of logical namespaces (`case`, `storage`, `mission`) and intended tools → downstream service IDs, URL env vars, and health paths |
| `GET /registry` | Serve the registry plus resolved (non-secret) downstream URLs |
| `GET /health` | Gateway liveness + **independent** downstream status, latency, and failure stage; reports exact `RAILWAY_GIT_COMMIT_SHA` |
| Request IDs | `X-Request-ID` / `X-Correlation-ID` middleware and logging foundation |
| Config validation | Fail closed on invalid timeouts or non-http(s) URLs |

## Failure isolation

- Downstream probes run concurrently; one failure cannot cancel the others.
- `/health` returns **HTTP 200** whenever the gateway process itself is up, with `"status": "ok" | "degraded"`.
- Namespace `capabilities` are derived only from that namespace’s downstream. An unhealthy Mission Control does not mark `case` or `storage` unavailable.

## Namespaces → downstreams

| Namespace | Downstream key | Default URL env |
| --- | --- | --- |
| `case` | `bridge` | `GATEWAY_BRIDGE_URL` |
| `storage` | `storage` | `GATEWAY_STORAGE_URL` |
| `mission` | `mission_control` | `GATEWAY_MISSION_CONTROL_URL` |

Artifact-routed tools (`get_artifacts`, `get_case_artifact`) also depend on
`GATEWAY_ARTIFACTS_URL` (`artifacts` service). Storage/artifacts default to the
current bridge public URL so Phase 1 works before those surfaces are split; set
the env vars to point at replacements without gateway code changes.

## Configuration

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `PORT` | no | `8080` | Listen port |
| `RAILWAY_GIT_COMMIT_SHA` | no | `unknown` | Exact deployed SHA when Railway injects it |
| `GATEWAY_HEALTH_TIMEOUT_SECONDS` | no | `5` | Per-probe timeout (`0.1`–`30`) |
| `GATEWAY_BRIDGE_URL` | no | production bridge URL from registry | Absolute `http(s)` base URL |
| `GATEWAY_STORAGE_URL` | no | same default as bridge | Independently replaceable |
| `GATEWAY_MISSION_CONTROL_URL` | no | production Mission Control URL | Absolute `http(s)` base URL |
| `GATEWAY_ARTIFACTS_URL` | no | same default as bridge | Independently replaceable |

## Local run

```bash
cd hal_legalai_gateway
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
PYTHONPATH=. uvicorn hal_legalai_gateway.server:app --host 0.0.0.0 --port 8080
```

Smoke:

```bash
curl -sS localhost:8080/health | python -m json.tool
curl -sS localhost:8080/registry | python -m json.tool
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
  hal-legalai-gateway
```

## Railway provisioning (manual)

This repository does **not** auto-create a new Railway service for the gateway.
`hal_legalai_gateway/railway.json` + `Dockerfile` are code-only deploy config.

**Manual steps (operator):**

1. In the Railway project that hosts Mission Control / the bridge, **create a new service**.
2. Point the service root / watch path at `hal_legalai_gateway` (Dockerfile builder).
3. Set env vars above as needed (`GATEWAY_*` overrides optional when registry defaults are correct).
4. Confirm Railway injects `RAILWAY_GIT_COMMIT_SHA`.
5. Expose public networking; health check path is `/health`.
6. Verify `GET /health` shows `deployed_commit_sha` equal to the deployed Git SHA and independent `downstream.*` entries.

Do not route ChatGPT / HAL production traffic through this gateway until a later phase adds authenticated tool proxying.

## Non-goals (Phase 1)

- No OAuth / MCP tool proxy
- No Case-00 Q1 generation, private benchmark access, or storage archive mutation
- No replacement of Mission Control, bridge, or B2 business logic
- No automatic Railway service creation from this repo alone
