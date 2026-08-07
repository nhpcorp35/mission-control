import json
import csv
from pathlib import Path
import sys


def find_json():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path("/Users/allenk443/Desktop/Legal-AI/judge_analytics.json")


def run():
    json_path = find_json()

    with open(json_path, "r") as f:
        data = json.load(f)

    rows = []

    for item in data:
        outcomes = item.get("outcomes", {})

        rows.append({
            "judge": item.get("judge"),
            "case_count": item.get("case_count", 0),
            "affirmed": outcomes.get("affirmed", 0),
            "reversed": outcomes.get("reversed", 0),
            "modified": outcomes.get("modified", 0),
            "granted": outcomes.get("granted", 0),
            "denied": outcomes.get("denied", 0),
            "dismissed": outcomes.get("dismissed", 0),
            "unknown": outcomes.get("unknown", 0),
        })

    out_path = json_path.parent / "judge_analytics.csv"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "judge",
                "case_count",
                "affirmed",
                "reversed",
                "modified",
                "granted",
                "denied",
                "dismissed",
                "unknown",
            ]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"✅ Saved: {out_path}")


if __name__ == "__main__":
    run()