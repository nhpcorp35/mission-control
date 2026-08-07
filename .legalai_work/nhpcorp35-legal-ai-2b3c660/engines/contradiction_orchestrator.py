from engines.contradiction_dedupe import dedupe_contradictions
from engines.contradiction_engine import detect_contradictions
from engines.contradiction_ranker import rank_contradictions
from engines.contradiction_reporter import build_contradiction_cards
from engines.contradiction_summary import summarize_contradictions


def build_contradiction_analysis(documents):
    findings = detect_contradictions(documents)

    findings = dedupe_contradictions(findings)

    ranked = rank_contradictions(findings)

    return {
        "summary": summarize_contradictions(ranked),
        "cards": build_contradiction_cards(ranked[:10]),
        "all_findings": ranked,
    }