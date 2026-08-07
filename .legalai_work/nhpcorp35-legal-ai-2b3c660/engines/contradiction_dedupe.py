from core.utils.contradiction_accessors import (
    get_contradiction_category,
    get_contradiction_source,
    get_contradiction_summary,
)


def dedupe_contradictions(items):
    unique = []
    seen = set()

    for item in items:
        source = get_contradiction_source(item)

        key = (
            get_contradiction_category(item),
            get_contradiction_summary(item),
            source.filename,
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    return unique