from core.utils.contradiction_accessors import (
    get_contradiction_category,
    get_contradiction_score,
    get_contradiction_source,
    get_contradiction_summary,
)


def build_contradiction_cards(items):
    cards = []

    for item in items:
        source = get_contradiction_source(item)

        cards.append(
            {
                "category": get_contradiction_category(item),
                "summary": get_contradiction_summary(item),
                "score": get_contradiction_score(item),
                "source_document": source.filename,
                "source_snippet": source.source_snippet,
            }
        )

    return cards