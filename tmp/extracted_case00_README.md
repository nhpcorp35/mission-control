# Case-00 Triborough — NYSCEF filing inventory

Canonical inventory: `nyscef_filing_inventory.json`

## Rebuild derived generator inputs (one command)

`scripts/generate_attorney_feedback_candidate.py` requires these Case-00 derived
files (plus the inventory and question packet / questions JSON):

- `derived/page-extraction/canonical_page_records.json`
- `derived/exhibit-segmentation/filing_exhibit_map.json`
- `derived/case-map/case_map.json`

Rebuild them deterministically from source PDFs via `matter_builder` ingestion
(no model provider):

```bash
# Local PDF directory
python scripts/rebuild_case00_derived.py \
  --case-root data/case-00-triborough \
  --source-dir /path/to/Tribrough\ Full\ Docket

# Or materialize the Case-00 B2 prefix, then ingest (uses B2_KEY_ID,
# B2_APPLICATION_KEY, B2_BUCKET, B2_ENDPOINT, B2_REGION — never printed)
python scripts/rebuild_case00_derived.py \
  --case-root data/case-00-triborough \
  --b2-prefix

# Validate generator-local inputs only (no rebuild, no model calls)
python scripts/rebuild_case00_derived.py \
  --case-root data/case-00-triborough \
  --validate-only
```

`--b2-prefix` without a value defaults to
`Benchmarks/Case-00-Triborough/original/Tribrough Full Docket/`.
Optional `--inventory-path` overrides the inventory under `--case-root`.
The rebuild overwrites only the three derived JSON files above; source PDFs and
attorney/gold/benchmark artifacts are never modified.

## Railway / executor configuration

Set these environment variables so LegalAI ingests the mounted Triborough corpus with verified NYSCEF page IDs:

```text
LEGALAI_MATTER_FOLDER=/app/data/case-00-triborough/source-pdfs/original:/Tribrough Full Docket
LEGALAI_NYSCEF_INVENTORY_PATH=data/case-00-triborough/nyscef_filing_inventory.json
```

Notes:

- `LEGALAI_MATTER_FOLDER` replaces the default `matter_docs` root for this executor only.
- `LEGALAI_NYSCEF_INVENTORY_PATH` must be set explicitly; unrelated matters do not load Triborough metadata.
- The misspelled `Tribrough` / `original:` volume segment is part of the mounted path and must be supplied via configuration, not hard-coded in application logic.
- `Archive.zip` remains excluded by allowed-extension filtering.

## Attorney-feedback evaluation

See `ATTORNEY_FEEDBACK_EVAL.md` for Case-00 evaluation, diagnostics, Railway
commit verification, and the one-command generate+evaluate workflow.

```bash
python -m case00_attorney_eval --help
python scripts/run_case00_generate_and_evaluate.py --help
```
