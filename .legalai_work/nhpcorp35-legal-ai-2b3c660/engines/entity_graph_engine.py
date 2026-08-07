import re


def build_entity_graph(documents):
    graph = {
        "entities": {
            "parties": [],
            "witnesses": [],
            "judges": [],
            "attorneys": [],
            "organizations": [],
            "exhibits": [],
            "dates": [],
            "procedural_events": [],
        },
        "relationships": [],
        "contradictions": [],
        "timeline": [],
    }

    for document in documents or []:
        text = (
            document.get("text")
            or document.get("content")
            or document.get("ocr_text")
            or ""
        )

        filename = document.get("filename", "Unknown Document")

        extract_exhibits(graph, text, filename)
        extract_dates(graph, text, filename)
        extract_judges(graph, text, filename)
        extract_procedural_events(graph, text, filename)

    return graph


def extract_exhibits(graph, text, filename):
    matches = re.findall(r"Exhibit\s+[A-Z]", text)

    for match in matches:
        graph["entities"]["exhibits"].append({
            "id": normalize_id(match),
            "name": match,
            "source_document": filename,
        })


def extract_dates(graph, text, filename):
    patterns = [
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text)

        for match in matches:
            graph["entities"]["dates"].append({
                "id": normalize_id(match),
                "name": match,
                "source_document": filename,
            })


def extract_judges(graph, text, filename):
    matches = re.findall(
        r"Hon\.\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+",
        text,
    )

    for match in matches:
        graph["entities"]["judges"].append({
            "id": normalize_id(match),
            "name": match,
            "source_document": filename,
        })


def extract_procedural_events(graph, text, filename):
    procedural_terms = [
        "motion to dismiss",
        "motion for summary judgment",
        "hearing",
        "conference",
        "order",
        "deposition",
    ]

    lowered = text.lower()

    for term in procedural_terms:
        if term in lowered:
            graph["entities"]["procedural_events"].append({
                "id": normalize_id(term),
                "name": term,
                "source_document": filename,
            })


def normalize_id(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")
