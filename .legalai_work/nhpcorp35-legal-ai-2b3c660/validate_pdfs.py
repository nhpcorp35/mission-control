import csv
import os

CSV_PATH = os.getenv("CSV_PATH", "output_clean.csv")
PDF_DIR = os.getenv("PDF_DIR", os.path.join("static", "pdfs"))
MIN_PDF_SIZE = int(os.getenv("MIN_PDF_SIZE", "1000"))


def normalize_case_number(raw):
    return str(raw or "").strip()


def get_case_number(row):
    return normalize_case_number(
        row.get("case_number")
        or row.get("case_no")
        or row.get("case")
        or row.get("index_number")
        or ""
    )


def find_pdf_for_case(case_number):
    if not case_number or not os.path.isdir(PDF_DIR):
        return None

    prefix = f"{case_number}__"

    for filename in os.listdir(PDF_DIR):
        if filename.lower().endswith(".pdf") and filename.startswith(prefix):
            return filename

    exact_name = f"{case_number}.pdf"
    exact_path = os.path.join(PDF_DIR, exact_name)
    if os.path.exists(exact_path):
        return exact_name

    return None


def check_pdf_header(path):
    try:
        with open(path, "rb") as f:
            header = f.read(5)
        return header == b"%PDF-"
    except Exception:
        return False


def load_case_numbers():
    case_numbers = []

    if not os.path.exists(CSV_PATH):
        return case_numbers

    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            case_number = get_case_number(raw)
            if case_number:
                case_numbers.append(case_number)

    return case_numbers


def validate_case_pdf(case_number):
    result = {
        "case_number": case_number,
        "status": "",
        "filename": None,
        "path": None,
        "size": None,
        "header_ok": False,
        "reason": "",
    }

    filename = find_pdf_for_case(case_number)

    if not filename:
        result["status"] = "missing"
        result["reason"] = "No matching PDF found"
        return result

    path = os.path.join(PDF_DIR, filename)
    result["filename"] = filename
    result["path"] = path

    if not os.path.exists(path):
        result["status"] = "missing"
        result["reason"] = "Matched filename but file missing on disk"
        return result

    size = os.path.getsize(path)
    result["size"] = size

    if size < MIN_PDF_SIZE:
        result["status"] = "suspicious"
        result["reason"] = f"File too small (< {MIN_PDF_SIZE} bytes)"

    header_ok = check_pdf_header(path)
    result["header_ok"] = header_ok

    if not header_ok:
        result["status"] = "invalid"
        result["reason"] = "Missing %PDF- header"
        return result

    if result["status"] == "suspicious":
        return result

    result["status"] = "valid"
    result["reason"] = "OK"
    return result


def write_report(results, output_path="pdf_validation_report.csv"):
    fieldnames = [
        "case_number",
        "status",
        "filename",
        "path",
        "size",
        "header_ok",
        "reason",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def main():
    print("\n=== PDF VALIDATION ===")
    print(f"CSV_PATH: {CSV_PATH}")
    print(f"PDF_DIR: {PDF_DIR}")
    print(f"MIN_PDF_SIZE: {MIN_PDF_SIZE}")

    if not os.path.exists(CSV_PATH):
        print("ERROR: CSV not found")
        return

    if not os.path.isdir(PDF_DIR):
        print("ERROR: PDF directory not found")
        return

    case_numbers = load_case_numbers()
    print(f"Cases in CSV: {len(case_numbers)}")

    results = []
    counts = {
        "valid": 0,
        "missing": 0,
        "invalid": 0,
        "suspicious": 0,
    }

    for case_number in case_numbers:
        result = validate_case_pdf(case_number)
        results.append(result)
        counts[result["status"]] += 1

    write_report(results)

    print("\nSummary:")
    print(f"  Valid:      {counts['valid']}")
    print(f"  Missing:    {counts['missing']}")
    print(f"  Invalid:    {counts['invalid']}")
    print(f"  Suspicious: {counts['suspicious']}")
    print("\nWrote: pdf_validation_report.csv")

    if counts["missing"] > 0:
        print("\nMissing PDFs:")
        for r in results:
            if r["status"] == "missing":
                print(f"  - {r['case_number']}")

    if counts["invalid"] > 0:
        print("\nInvalid PDFs:")
        for r in results:
            if r["status"] == "invalid":
                print(f"  - {r['case_number']} -> {r['filename']}")

    if counts["suspicious"] > 0:
        print("\nSuspicious PDFs:")
        for r in results:
            if r["status"] == "suspicious":
                print(f"  - {r['case_number']} -> {r['filename']} ({r['size']} bytes)")

    print("\n======================\n")


if __name__ == "__main__":
    main()
