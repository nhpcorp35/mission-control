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

The Case-00 path accepts only an exact 40-character commit SHA and requires
explicit authorization before private evidence can be transmitted to the model
provider. The GitHub workflow is generation-only. It rebuilds permitted Case-00
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

Mission Control remains unchanged and is not used by this bridge.

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
