from core.models import (
    ContradictionFinding,
    DocumentReference,
)


def get_contradiction_score(item):
    if isinstance(item, ContradictionFinding):
        return item.score

    if isinstance(item, dict):
        return item.get("score", 0)

    return 0


def get_contradiction_category(item):
    if isinstance(item, ContradictionFinding):
        return item.category

    if isinstance(item, dict):
        return item.get("category", "")

    return ""


def get_contradiction_summary(item):
    if isinstance(item, ContradictionFinding):
        return item.summary

    if isinstance(item, dict):
        return item.get("summary", "")

    return ""


def get_contradiction_source(item):
    if isinstance(item, ContradictionFinding):
        return item.source or DocumentReference()

    if isinstance(item, dict):
        return DocumentReference(
            filename=item.get("source_document", ""),
            document_type=item.get("source_type", ""),
            source_snippet=item.get("source_snippet", ""),
        )

    return DocumentReference()