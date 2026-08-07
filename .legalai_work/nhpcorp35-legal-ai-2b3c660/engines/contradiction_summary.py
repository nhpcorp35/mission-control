from core.utils.contradiction_accessors import (
    get_contradiction_category,
    get_contradiction_score,
)


def summarize_contradictions(items):
    if not items:
        return {
            "total": 0,
            "highest_score": 0,
            "categories": [],
        }

    categories = sorted(
        {
            get_contradiction_category(item)
            for item in items
        }
    )

    highest_score = max(
        get_contradiction_score(item)
        for item in items
    )

    return {
        "total": len(items),
        "highest_score": highest_score,
        "categories": categories,
    }