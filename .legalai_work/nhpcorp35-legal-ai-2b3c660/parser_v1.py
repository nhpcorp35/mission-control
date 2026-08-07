import re
import json
import sys
import subprocess
from pathlib import Path


# --- ROOT ---
def find_pdf_root():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path.cwd()


# --- TEXT EXTRACTION ---
def extract_text(pdf_path):
    text = ""

    # 1️⃣ pdftotext
    try:
        result = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            capture_output=True,
            text=True
        )
        text = result.stdout
    except:
        pass

    # 2️⃣ fallback: pdfminer
    if not text.strip():
        try:
            from pdfminer.high_level import extract_text as miner_extract
            text = miner_extract(str(pdf_path))
        except:
            pass

    return text


# --- FILENAME ---
def parse_filename(name):
    parts = name.replace(".pdf", "").split("__")

    case_id = parts[0]
    date = parts[2] if len(parts) > 2 else None

    if case_id.startswith("M-"):
        return None, case_id, date
    return case_id, None, date


# --- OUTCOME ---
def detect_outcome(text):
    t = text.lower()

    for w in ["affirmed", "reversed", "modified", "granted", "denied", "dismissed"]:
        if w in t:
            return w

    return None


# --- COURT ---
def detect_court(text):
    if "appellate division" in text.lower():
        return "Appellate Division, First Department"
    return None


# --- CITATIONS (FIXED SPACING) ---
def extract_citations(text):
    t = re.sub(r"\s+", " ", text.lower())

    slip = re.findall(r"\b\d{4}\s+ny slip op\s+\d+\b", t)
    rep = re.findall(r"\b\d+\s+ad\d*d?\s+\d+\b", t)

    return {
        "slip_op": list(set(slip)),
        "reporters": list(set(rep))
    }


# --- JUDGES (CLEAN) ---
BAD = {
    "for", "order", "judgment", "entered", "rendered",
    "llp", "pllc", "admitted", "southfield"
}


def extract_judges(text):
    lines = text.split("\n")[:50]
    judges = []

    for line in lines:
        if "J." not in line:
            continue

        clean = re.sub(r"\b(j\.p\.|p\.j\.|jj\.|j\.)\b", "", line, flags=re.I)
        parts = [p.strip(" ,.;:") for p in clean.split(",")]

        for p in parts:
            name = p.strip().lower()

            if name in BAD:
                continue

            if len(name) < 3:
                continue

            if not re.fullmatch(r"[a-z'\-]+", name):
                continue

            judges.append(p.capitalize() + ", J.")

    # dedupe while preserving order
    return list(dict.fromkeys(judges))


# --- PARTIES ---
def extract_parties(text):
    match = re.search(
        r"([A-Z][A-Za-z ,.'&\-]+?)\s+v\.?\s+([A-Z][A-Za-z ,.'&\-]+)",
        text
    )

    if match:
        p1 = match.group(1).strip(" ,.;:")
        p2 = match.group(2).strip(" ,.;:")
        return [p1, p2]

    return []


# --- MAIN ---
def parse_pdf(pdf_path):
    text = extract_text(pdf_path)

    case_number, motion_number, date = parse_filename(pdf_path.name)

    return {
        "file": pdf_path.name,
        "case_number": case_number,
        "motion_number": motion_number,
        "court": detect_court(text),
        "date": date,
        "outcome": detect_outcome(text),
        "citations": extract_citations(text),
        "judges": extract_judges(text),
        "parties": extract_parties(text),
    }


# --- RUN ---
def run():
    root = find_pdf_root()

    pdfs = list(root.rglob("*.pdf"))
    print(f"📄 Found {len(pdfs)} PDFs")

    results = [parse_pdf(p) for p in pdfs]

    out = root / "output_v3.json"

    with open(out, "w") as f:
        json.dump(results, f, indent=2)

    print(f"✅ Saved → {out}")


if __name__ == "__main__":
    run()