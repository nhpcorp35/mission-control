from core.utils.contradiction_accessors import (
    get_contradiction_category,
    get_contradiction_score,
)


def sort_contradictions(items):
    return sorted(
        items,
        key=lambda item: (
            get_contradiction_score(item),
            get_contradiction_category(item),
        ),
        reverse=True,
    )