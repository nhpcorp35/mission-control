import json
from pathlib import Path
from collections import defaultdict, Counter
import sys


def find_json():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    return Path("/Users/allenk443/Desktop/Legal-AI/output_v3.json")


def load_data(json_path):
    with open(json_path, "r") as f:
        return json.load(f)


def normalize_judge_name(name):
    if not name:
        return None

    name = str(name).strip()

    # keep as-is except light cleanup
    name = " ".join(name.split())

    return name or None


def build_judge_stats(cases):
    judge_stats = defaultdict(lambda: {
        "case_count": 0,
        "outcomes": Counter(),
        "cases": []
    })

    for case in cases:
        outcome = case.get("outcome") or "unknown"
        judges = case.get("judges", [])
        case_id = case.get("case_number") or case.get("motion_number") or case.get("file")
        date = case.get("date")
        file_name = case.get("file")

        # avoid double counting the same judge twice in one case
        clean_judges = []
        seen = set()

        for judge in judges:
            j = normalize_judge_name(judge)
            if j and j not in seen:
                seen.add(j)
                clean_judges.append(j)

        for judge in clean_judges:
            judge_stats[judge]["case_count"] += 1
            judge_stats[judge]["outcomes"][outcome] += 1
            judge_stats[judge]["cases"].append({
                "case": case_id,
                "date": date,
                "outcome": outcome,
                "file": file_name,
            })

    return judge_stats


def print_summary(judge_stats):
    print("\nJUDGE ANALYTICS\n")
    print("=" * 80)

    sorted_judges = sorted(
        judge_stats.items(),
        key=lambda x: (-x[1]["case_count"], x[0])
    )

    for judge, stats in sorted_judges:
        outcome_parts = []
        for outcome, count in sorted(stats["outcomes"].items()):
            outcome_parts.append(f"{outcome}: {count}")

        outcome_text = ", ".join(outcome_parts)

        print(f"{judge}")
        print(f"  cases: {stats['case_count']}")
        print(f"  outcomes: {outcome_text}")
        print("-" * 80)


def save_json_report(judge_stats, out_path):
    report = []

    sorted_judges = sorted(
        judge_stats.items(),
        key=lambda x: (-x[1]["case_count"], x[0])
    )

    for judge, stats in sorted_judges:
        report.append({
            "judge": judge,
            "case_count": stats["case_count"],
            "outcomes": dict(stats["outcomes"]),
            "cases": stats["cases"],
        })

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)


def run():
    json_path = find_json()
    print(f"Reading: {json_path}")

    cases = load_data(json_path)
    judge_stats = build_judge_stats(cases)

    print_summary(judge_stats)

    out_path = json_path.parent / "judge_analytics.json"
    save_json_report(judge_stats, out_path)

    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    run()