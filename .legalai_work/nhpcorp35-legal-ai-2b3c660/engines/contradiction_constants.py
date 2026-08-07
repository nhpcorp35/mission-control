ENGINE_VERSION = "Contradiction Engine v1.0"


CONTRADICTION_PATTERNS = [
    (
        "timeline_conflict",
        [
            r"before",
            r"after",
            r"later",
            r"earlier",
            r"same day",
        ],
    ),
    (
        "notice_conflict",
        [
            r"notice",
            r"aware",
            r"knowledge",
            r"informed",
        ],
    ),
    (
        "possession_conflict",
        [
            r"owned",
            r"possessed",
            r"control",
            r"custody",
        ],
    ),
]