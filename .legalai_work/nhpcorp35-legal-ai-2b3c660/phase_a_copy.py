from pathlib import Path
import shutil

RAW_DIR = Path("data/raw")
STATIC_DIR = Path("static/pdfs")

copied = 0
errors = 0

STATIC_DIR.mkdir(parents=True, exist_ok=True)

if not RAW_DIR.exists():
    print(f"⚠️ Raw PDF dir not found: {RAW_DIR}")
    print("SUMMARY_COPIED=0")
    print("SUMMARY_ERRORS=0")
    raise SystemExit(0)

for pdf_path in sorted(RAW_DIR.rglob("*.pdf")):
    dest = STATIC_DIR / pdf_path.name
    if dest.exists():
        continue
    try:
        shutil.copy2(pdf_path, dest)
        copied += 1
        print(f"Copied: {pdf_path} -> {dest}")
    except Exception as e:
        errors += 1
        print(f"Error copying {pdf_path}: {e}")

print(f"✅ Phase A copy complete. Copied {copied} PDFs.")
print(f"SUMMARY_COPIED={copied}")
print(f"SUMMARY_ERRORS={errors}")
