import re

from core.models import (
    ContradictionFinding,
    DocumentReference,
)

from core.utils.scoring import clamp_score

from engines.contradiction_constants import (
    CONTRADICTION_PATTERNS,
    ENGINE_VERSION,
)


def clean_text(value):
    if not value:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def detect_contradictions(documents):
    findings = []

    for doc in documents:
        text = clean_text(doc.get("text"))

        if not text:
            continue

        lowered = text.lower()

        for category, patterns in CONTRADICTION_PATTERNS:
            matches = [
                pattern
                for pattern in patterns
                if re.search(pattern, lowered)
            ]

            if len(matches) < 2:
                continue

            findings.append(
                ContradictionFinding(
                    category=category,
                    summary=f"Potential {category.replace('_', ' ')} detected.",
                    score=clamp_score(70 + (len(matches) * 5)),
                    source=DocumentReference(
                        filename=doc.get("filename", ""),
                        document_type=doc.get("type", ""),
                        source_snippet=text[:400],
                    ),
                )
            )

    return findings