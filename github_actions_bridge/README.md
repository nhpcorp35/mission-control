# HAL GitHub Actions bridge

This isolated OAuth-protected service exposes the original four proof tools,
four bounded LegalAI Case-00 Q1 tools, and three controlled Case-00 storage tools.

Proof tools:

- `submit_run`
- `get_run`
- `cancel_run`
- `get_artifacts`

Case-00 Q1 tools:

- `submit_case00_q1`
- `get_case00_q1_run`
- `cancel_case00_q1_run`
- `get_case00_q1_artifacts`

Case-00 storage tools:

- `list_case00_storage`
- `archive_case00_attorney_feedback`
- `archive_case00_review_packet`

Generic acceptance-contract storage tools:

- `archive_acceptance_contract`
- `verify_acceptance_contract`
- `list_acceptance_contracts`

The Case-00 path accepts the configured workflow branch alias (normally
`main`) or an exact 40-character lowercase commit SHA, and requires
explicit authorization before private evidence can be transmitted to the model
provider. Branch aliases are resolved to HEAD of the configured
`GITHUB_REPOSITORY` (the LegalAI workflow repository) via the GitHub API;
explicit SHAs are preflight-checked in that same repository before
`workflow_dispatch`. Arbitrary branches, tags, abbreviated SHAs, uppercase
SHAs, and commits absent from LegalAI are rejected with structured
`error_code` values (`ref_invalid`, `ref_not_in_repository`,
`ref_resolution_failed`, `dispatch_failed`) and no workflow is created.
Successful submits return `requested_ref`, `resolved_ref`, `repository`, and
`workflow`. GitHub Actions always receives the verified immutable SHA. The
GitHub workflow is generation-only. It rebuilds permitted Case-00
inputs from B2, generates Q1 with the production path, uploads exactly four
candidate artifacts under the canonical candidate prefix, and fails unless all
four objects pass B2 HEAD verification. The artifact retrieval tool independently
HEAD-verifies those objects before returning their keys.

The storage inventory tool can list metadata only beneath named canonical
Case-00 prefixes (including `attorney_review_packets`) and caps each response at
200 objects. The attorney-feedback tool accepts only the fixed three-file review
package, generates a preservation manifest server-side, stores the package
beneath a deterministic Case-00 review prefix, and verifies every object by size
and SHA-256 metadata. The review-packet tool accepts exact DOCX bytes as strict
base64 plus a bounded syntactically valid recipient email (normalized to
lowercase), question_id, sent_at, and original filename
metadata; validates ZIP/OOXML structure; stores the unchanged DOCX and a
server-generated preservation manifest beneath
`Benchmarks/Case-00-Triborough/derived/attorney-feedback-eval/attorney-review-packets/<archive_id>/`;
rejects collisions instead of overwriting; and returns `verified: true` only
after HEAD size and SHA-256 metadata checks. None of these tools permit
arbitrary bucket names or object keys. Private benchmark data and generated
packet content are not stored in GitHub.

The acceptance-contract tools archive a LegalAI `acceptance_contract.v1` JSON
object (transport-safe base64) beneath
`Benchmarks/acceptance-contracts/`. They accept nested
`identity{benchmark_id,question_id}`, required rule blocks, and
`content_sha256`, reject the obsolete flat schema shape, match LegalAI
canonical `contract_sha256` (sorted compact UTF-8 JSON excluding
`content_sha256`) plus exact serialized `object_sha256`, refuse non-canonical
buckets and path traversal, reject overwrites with different content, and
return `verified: true` only after HEAD size and both integrity checks.
Independent `verify_acceptance_contract` and `list_acceptance_contracts`
confirm objects by key/size/`contract_sha256`/`object_sha256` metadata without
returning contract contents. Credentials are never accepted or logged.

Mission Control remains unchanged for the original GitHub Actions tools.
Namespaced `mission.*` / `workflow.*` catalog tools on public `/mcp` are a
thin compatibility forward to the canonical HAL LegalAI Gateway MCP (or, when
that URL is unset, Mission Control MCP — the same downstream the gateway
already uses). No mission YAML parsing or workflow orchestration runs here.

## Authentication (two MCP surfaces)

| Path | Audience | Auth |
| --- | --- | --- |
| `POST/GET /mcp` | Direct ChatGPT / operator clients | FastMCP `GitHubProvider` OAuth (unchanged) |
| `POST/GET /mcp/service` | HAL LegalAI Gateway only | `BRIDGE_SERVICE_TOKEN` via FastMCP 2.x `TokenVerifier` (static bearer). Fail closed. **No** GitHub OAuth discovery on this path. |

Do **not** put a composite OAuth+service verifier on the public `/mcp` route.
Gateway must call `/mcp/service` with `GATEWAY_BRIDGE_AUTHORIZATION` matching
`BRIDGE_SERVICE_TOKEN` (with or without a `Bearer ` prefix). Inbound user OAuth
session tokens must never be forwarded downstream.

When `BRIDGE_SERVICE_TOKEN` is unset, `/mcp/service` rejects all credentials
(fail closed). Public `/mcp` OAuth behavior is unchanged.

### ChatGPT plugin Refresh (bridge-backed HAL LegalAI Gateway record)

The unnumbered ChatGPT connector named **HAL LegalAI Gateway** is keyed to this
bridge origin, not the separate `hal-legalai-gateway` Railway service. Do
**not** delete or recreate that record. One-click Refresh recaches `tools/list`
from the same OAuth-protected MCP URL.

| Discovery field | Value (must stay unchanged) |
| --- | --- |
| Plugin / MCP resource | `https://hal-github-actions-bridge-production.up.railway.app/mcp` |
| RFC 9728 metadata | `https://hal-github-actions-bridge-production.up.railway.app/.well-known/oauth-protected-resource/mcp` |
| OAuth authorization server | `https://hal-github-actions-bridge-production.up.railway.app/.well-known/oauth-authorization-server` |
| RFC 9728 `resource` | the `/mcp` URL above |
| RFC 9728 `resource_name` | `HAL LegalAI Gateway` |

**One-click Refresh verification**

1. Deploy this bridge at the existing `BRIDGE_PUBLIC_URL` (do not change
   domain, GitHub OAuth app, secrets, or `/mcp`).
2. Confirm `GET /health` (no auth) reports `catalog_identity=HAL LegalAI Gateway`,
   `plugin_refresh_mcp_url` ending in `/mcp`, legacy tools (`submit_run`,
   `submit_case00`, `list_case00_storage`, …), and canonical names
   `mission.submit`, `workflow.submit`, `workflow.status`, `case.submit` /
   `case.get_artifact`, `storage.list_inventory`.
3. In ChatGPT, open **developer information** for the existing connector whose
   MCP URL is `https://hal-github-actions-bridge-production.up.railway.app/mcp`
   (display label **HAL LegalAI Gateway**). Use in-place **Refresh**. Do not
   reconnect a new URL and do not add a second connector.
4. If Refresh asks to re-authorize GitHub OAuth, complete it against this same
   origin. The OAuth resource URL must remain `{BRIDGE_PUBLIC_URL}/mcp`.
5. Start a **new chat** bound to that same connector. Confirm `tools/list`
   includes namespaced `case.*`, `storage.*`, `mission.submit`,
   `workflow.submit`, and `workflow.status` **and** the original proof / Case-00
   / storage tool names.
6. Fail closed: unauthenticated `/mcp` stays 401; `/mcp/service` still requires
   `BRIDGE_SERVICE_TOKEN` and must not grow GitHub OAuth discovery.

Optional `HAL_LEGALAI_GATEWAY_URL` (canonical gateway origin) plus existing
`BRIDGE_SERVICE_TOKEN` forwards namespaced mission/workflow calls to gateway
`/mcp`. When that URL is unset, those tools forward to Mission Control MCP at
`MISSION_CONTROL_MCP_URL` (default
`https://mission-control-mcp-production.up.railway.app/mcp`). Downstream and
auth failures return `ok=false` with a `failure_stage`; inbound OAuth is never
copied downstream. Case/storage namespaced tools alias local implementations so
they do not round-trip through the gateway (avoids a Bridge→Gateway→Bridge loop).

### Railway / cutover verification

| Path | Audience | Auth |
| --- | --- | --- |
| `POST/GET /mcp` | Direct ChatGPT / operator clients | FastMCP `GitHubProvider` OAuth (unchanged) |
| `POST/GET /mcp/service` | HAL LegalAI Gateway only | `BRIDGE_SERVICE_TOKEN` via FastMCP 2.x `TokenVerifier` (static bearer). Fail closed. **No** GitHub OAuth discovery on this path. |

Do **not** put a composite OAuth+service verifier on the public `/mcp` route.
Gateway must call `/mcp/service` with `GATEWAY_BRIDGE_AUTHORIZATION` matching
`BRIDGE_SERVICE_TOKEN` (with or without a `Bearer ` prefix). Inbound user OAuth
session tokens must never be forwarded downstream.

When `BRIDGE_SERVICE_TOKEN` is unset, `/mcp/service` rejects all credentials
(fail closed). Public `/mcp` OAuth behavior is unchanged.

### Railway / cutover verification

1. Set `BRIDGE_SERVICE_TOKEN` on the Bridge service (same value as Gateway
   `GATEWAY_BRIDGE_AUTHORIZATION`; no new secret type required). **Do not
   rotate again** if both services were already redeployed with the same
   synchronized 96-hex value — a 401 after that is a delivery bug, not
   credential drift.
2. Ensure Gateway `GATEWAY_MCP_PATH` is `/mcp/service` (the deterministic
   default) for Bridge / Storage / Artifacts forwarding.
3. Confirm Gateway forwarding uses FastMCP `StreamableHttpTransport(auth=raw_token)`
   (not a manually injected `Authorization` header). Inbound GitHub OAuth must
   never be forwarded downstream.
4. Confirm `GET /health` still works without auth.
5. Confirm public OAuth clients still use `/mcp` only.
6. Confirm Gateway `storage.list_inventory` succeeds through `/mcp/service`.
7. Confirm missing/invalid service bearer returns HTTP 401 on `/mcp/service`.
8. Confirm Bridge verifier logs distinguish `missing_bearer=True` vs mismatch
   (`provided_len` / `expected_len` / short SHA fingerprint only — never raw
   tokens).
9. Confirm logs and error bodies never echo the service token.
