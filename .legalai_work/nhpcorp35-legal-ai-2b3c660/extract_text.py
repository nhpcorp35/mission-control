import argparse
import csv
import subprocess
from pathlib import Path

PDF_DIR = Path("static/pdfs")
INPUT_CSV = Path("output_clean.csv")
OUTPUT_CSV = Path("output_enriched.csv")
MAX_TEXT_LEN = 20000


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["full", "incremental"],
        default="full",
        help="full = rebuild output_enriched.csv, incremental = append only new rows",
    )
    return parser.parse_args()


def get_text(pdf_path):
    try:
        result = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=True,
        )
        text = result.stdout or ""
        if text.strip():
            return text
    except Exception:
        pass

    try:
        from pdfminer.high_level import extract_text as pdf_extract_text
        text = pdf_extract_text(pdf_path)
        if text.strip():
            return text
    except Exception as e:
        print(f"Error reading {pdf_path}: {e}")

    return ""


def find_pdf(case_number, motion_number):
    case_number = (case_number or "").strip()
    motion_number = (motion_number or "").strip()

    if case_number:
        for pdf in PDF_DIR.glob("*.pdf"):
            if pdf.name.startswith(case_number):
                return pdf

    if motion_number:
        for pdf in PDF_DIR.glob("*.pdf"):
            if pdf.name.startswith(motion_number):
                return pdf

    return None


def load_csv_rows(path):
    if not path.exists():
        return [], []

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    return rows, fieldnames


def write_csv_rows(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def unique_key(row):
    case_number = (row.get("case_number") or "").strip()
    motion_number = (row.get("motion_number") or "").strip()
    if case_number:
        return f"case:{case_number}"
    if motion_number:
        return f"motion:{motion_number}"
    return ""


def main():
    args = get_args()

    if not INPUT_CSV.exists():
        print(f"❌ Missing input CSV: {INPUT_CSV}")
        print("SUMMARY_APPENDED_ROWS=0")
        print("SUMMARY_TOTAL_ROWS=0")
        print("SUMMARY_ERRORS=1")
        raise SystemExit(1)

    input_rows, input_fieldnames = load_csv_rows(INPUT_CSV)

    existing_rows = []
    existing_fieldnames = []
    existing_keys = set()

    if args.mode == "incremental" and OUTPUT_CSV.exists():
        existing_rows, existing_fieldnames = load_csv_rows(OUTPUT_CSV)
        existing_keys = {unique_key(r) for r in existing_rows if unique_key(r)}

    appended_rows = []
    errors = 0
    missing_pdf = 0

    for row in input_rows:
        row_key = unique_key(row)
        if not row_key:
            continue

        if args.mode == "incremental" and row_key in existing_keys:
            continue

        case_number = row.get("case_number")
        motion_number = row.get("motion_number")
        pdf_file = find_pdf(case_number, motion_number)

        if not pdf_file:
            missing_pdf += 1
            continue

        label = (case_number or "").strip() or (motion_number or "").strip()
        print(f"Processing {label} -> {pdf_file.name}")

        text = get_text(pdf_file)
        if not text.strip():
            errors += 1
            continue

        out_row = dict(row)
        out_row["full_text"] = text[:MAX_TEXT_LEN]
        appended_rows.append(out_row)

    if args.mode == "full":
        if not appended_rows:
            print("❌ No rows processed")
            print("SUMMARY_APPENDED_ROWS=0")
            print("SUMMARY_TOTAL_ROWS=0")
            print(f"SUMMARY_ERRORS={errors}")
            raise SystemExit(1)

        fieldnames = list(appended_rows[0].keys())
        write_csv_rows(OUTPUT_CSV, appended_rows, fieldnames)

        print(f"✅ Wrote {OUTPUT_CSV} with {len(appended_rows)} rows")
        print(f"SUMMARY_APPENDED_ROWS={len(appended_rows)}")
        print(f"SUMMARY_TOTAL_ROWS={len(appended_rows)}")
        print(f"SUMMARY_ERRORS={errors}")
        print(f"SUMMARY_MISSING_PDF={missing_pdf}")
        return

    if existing_fieldnames:
        fieldnames = existing_fieldnames[:]
    elif input_fieldnames:
        fieldnames = input_fieldnames[:]
    else:
        fieldnames = []

    if "full_text" not in fieldnames:
        fieldnames.append("full_text")

    final_rows = existing_rows + appended_rows

    if final_rows:
        write_csv_rows(OUTPUT_CSV, final_rows, fieldnames)

    total_rows = len(final_rows)

    print(f"✅ Incremental update complete. Appended {len(appended_rows)} rows. Total now {total_rows}.")
    print(f"SUMMARY_APPENDED_ROWS={len(appended_rows)}")
    print(f"SUMMARY_TOTAL_ROWS={total_rows}")
    print(f"SUMMARY_ERRORS={errors}")
    print(f"SUMMARY_MISSING_PDF={missing_pdf}")


if __name__ == "__main__":
    main()
