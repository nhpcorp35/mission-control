from core.models import (
    DocumentReference,
    IssueFinding,
)


RISK_WEIGHTS = {
    "high": 3,
    "medium": 2,
    "low": 1,
}


def get_issue_score(item):
    if isinstance(item, IssueFinding):
        return item.score

    if isinstance(item, dict):
        return item.get("score", 0)

    return 0


def get_issue_label(item):
    if isinstance(item, IssueFinding):
        return item.issue

    if isinstance(item, dict):
        return item.get("issue", "")

    return ""


def get_issue_category(item):
    if isinstance(item, IssueFinding):
        return item.category

    if isinstance(item, dict):
        return item.get("category", "")

    return ""


def get_issue_risk_level(item):
    if isinstance(item, IssueFinding):
        return item.risk_level

    if isinstance(item, dict):
        return item.get("risk_level", "medium")

    return "medium"


def get_issue_risk_weight(item):
    return RISK_WEIGHTS.get(get_issue_risk_level(item), 0)


def get_issue_focus(item):
    if isinstance(item, IssueFinding):
        return item.recommended_focus

    if isinstance(item, dict):
        return item.get("recommended_focus", "")

    return ""


def get_issue_source(item):
    if isinstance(item, IssueFinding):
        return item.source or DocumentReference()

    if isinstance(item, dict):
        return DocumentReference(
            filename=item.get("source_document", ""),
            document_type=item.get("source_type", ""),
            source_snippet=item.get("source_snippet", ""),
        )

    return DocumentReference()
