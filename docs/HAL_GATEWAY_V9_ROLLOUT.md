# HAL LegalAI Gateway v9 rollout

## Purpose

ChatGPT's published Gateway v8 tool snapshot does not expose every tool now
registered on the deployed Gateway. A new ChatGPT app registration is required
to expose a changed tool catalog. Keep v8 connected until the v9 app has passed
authenticated live read-only checks.

## Exact additions

| Tool | Current backend | v9 behavior |
| --- | --- | --- |
| `case.framework_evidence_check` | Gateway HTTP route to Bridge | Already registered; republish for read-only, no-model analysis. |
| `activity.poll` | Gateway to Bridge | Already registered; republish for bounded activity monitoring. |
| `authority.reviewed.get` | Bridge MCP | New Gateway forwarder; reads one case/source-bound, hash-verified B2 record. |
| `authority.reviewed.put` | Bridge MCP | New Gateway forwarder; explicit `authorization_confirmed` required before forwarding. Bridge enforces immutable reviewed-authority contract. Do not call during rollout verification. |

All existing Gateway bindings remain. The Gateway uses its service credential
for Bridge calls; it never forwards the caller's GitHub OAuth bearer. The
Gateway still requires its configured allowed GitHub login.

## Release and verification

1. Merge the tested Gateway and registry change; deploy the Gateway service.
   Confirm its health reports the new commit and the two authority bindings.
2. Confirm Bridge health and `system.capabilities` still expose
   `authority.reviewed.get` and `authority.reviewed.put`.
3. Register a **new** ChatGPT MCP app named `HAL LegalAI Gateway v9` against the
   same Gateway MCP endpoint. Review the new OAuth client grant explicitly.
   Do not replace or disconnect v8 yet.
4. In v9, call `gateway.auth_status`, `gateway.health`,
   `authority.reviewed.get` for an already verified Rennick record, and
   `case.framework_evidence_check` with an existing verified source hash.
   Compare returned hash and record count with the prior executor readback.
   Do not invoke the write tool, paid drafts, or source-record mutations.
5. Only after those checks pass, move routine work to v9. Preserve v8 until
   no in-flight task depends on it. A future Gateway code change that alters
   the published tool schema requires another app registration.

## Limits

- This change does not fix GitHub authorization or create a new ChatGPT app.
- No B2 source object or attorney draft is altered by deployment or catalog
  discovery. `authority.reviewed.put` is a separately authorized future action.
- The Registry v53 historical Q2/Q5 label is outside this Gateway change.
