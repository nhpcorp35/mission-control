from core.utils.contradiction_accessors import (
    get_contradiction_score,
)


def rank_contradictions(items):
    return sorted(
        items,
        key=lambda item: get_contradiction_score(item),
        reverse=True,
    )