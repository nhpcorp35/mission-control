# HAL GitHub Actions bridge

This isolated OAuth-protected service exposes the original four proof tools and
four bounded LegalAI Case-00 Q1 tools.

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

The Case-00 path accepts only an exact 40-character commit SHA and requires
explicit authorization before private evidence can be transmitted to the model
provider. The GitHub workflow is generation-only. It rebuilds permitted Case-00
inputs from B2, generates Q1 with the production path, uploads exactly four
candidate artifacts under the canonical candidate prefix, and fails unless all
four objects pass B2 HEAD verification. The artifact retrieval tool independently
HEAD-verifies those objects before returning their keys.

Mission Control remains unchanged and is not used by this bridge.
