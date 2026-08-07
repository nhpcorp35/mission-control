import json
import csv
from pathlib import Path
import sys


def find_json():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path.cwd() / "output_v3.json"


def clean(s):
    if not s:
        return ""
    return str(s).replace("\n", " ").strip()


def flatten_case(case):
    return {
        "case_number": clean(case.get("case_number")),
        "motion_number": clean(case.get("motion_number")),
        "date": clean(case.get("date")),
        "court": clean(case.get("court")),
        "outcome": clean(case.get("outcome")),

        "judges": clean("; ".join(case.get("judges", []))),
        "parties": clean("; ".join(case.get("parties", []))),
        "slip_op": clean("; ".join(case.get("citations", {}).get("slip_op", []))),
        "reporters": clean("; ".join(case.get("citations", {}).get("reporters", []))),
    }


def run():
    json_path = find_json()

    with open(json_path, "r") as f:
        data = json.load(f)

    rows = [flatten_case(c) for c in data]

    out_path = json_path.parent / "output_clean.csv"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "case_number",
                "motion_number",
                "date",
                "court",
                "outcome",
                "judges",
                "parties",
                "slip_op",
                "reporters",
            ]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ Clean CSV → {out_path}")


if __name__ == "__main__":
    run()