
# Legal AI Project Map

## Pipeline

Scraper (downloads PDFs):

* node scraper/scraper.js

Phase A (copy to static):

* python3 phase_a_copy.py

Phase B (text extraction → CSV):

* python3 extract_text.py

Incremental pipeline:

* ./pipeline/run_incremental.sh

---

## Data Locations

Raw PDFs:

* data/raw/

Served PDFs:

* static/pdfs/

Base dataset:

* output_clean.csv

Enriched dataset:

* output_enriched.csv

---

## Notes

* Scraper uses download_tracker.json for dedup (source of truth)
* Extract script supports:

  * full mode (rebuild CSV)
  * incremental mode (append only new cases)
