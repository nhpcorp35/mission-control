# HAL GitHub Actions bridge proof

This isolated service exposes four bounded MCP tools for the LegalAI proof:

- `submit_run`
- `get_run`
- `cancel_run`
- `get_artifacts`

It dispatches only `.github/workflows/hal-bridge-proof.yml` in
`nhpcorp35/legal-ai`. After a successful run, `get_artifacts` downloads the
workflow's harmless `proof.json`, writes it under `Benchmarks/Bridge-Proof/` in
Backblaze B2, verifies the object with `head_object`, and returns its key.

