# engines/issue_engine.py

import re


ENGINE_VERSION = "Issue Engine v3.4 — Source Traceability"


REQUIRED_DOCUMENT_TYPES = {
    "summary judgment motion": ["motion", "memo", "affirmation", "opposition"],
    "motion to dismiss": ["motion", "memo", "complaint", "opposition"],
    "default judgment motion": ["motion", "affirmation", "complaint"],
}


BURDEN_RULES = {
    "summary judgment motion": [
        "Movant must establish prima facie entitlement to judgment as a matter of law.",
        "Failure to satisfy the initial burden requires denial regardless of opposition proof.",
        "Triable issues of fact defeat summary judgment.",
    ],
    "motion to dismiss": [
        "Pleadings should be liberally construed.",
        "The Court must accept allegations as true at this stage.",
        "Documentary evidence must conclusively dispose of claims.",
    ],
}


ALLEGATION_PATTERNS = [
    r"plaintiff alleges",
    r"defendant breached",
    r"failed to",
    r"negligently",
    r"wrongfully",
    r"caused",
    r"damages",
    r"notice",
]


WEAK_PHRASES = [
    "upon information and belief",
    "approximately",
    "appears to",
    "may have",
    "possibly",
    "unknown",
    "to be determined",
]


ATTACK_KEYWORDS = {
    "standing": "Standing challenge may be dispositive.",
    "jurisdiction": "Jurisdictional defects may defeat the action.",
    "notice": "Notice allegations may be factually insufficient.",
    "causation": "Causation proof may be incomplete or speculative.",
    "damages": "Damages proof may be unsupported.",
    "breach": "Breach allegations should be tested against documentary proof.",
    "contract": "Contract interpretation may control the outcome.",
    "discovery": "Discovery deficiencies may support procedural attack.",
    "documentary evidence": "Documentary evidence may contradict the allegations.",
}


DATE_PATTERNS = [
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
]


FACT_RISK_TERMS = [
    "approximately",
    "unknown",
    "estimate",
    "unclear",
    "cannot recall",
    "believed",
]


CREDIBILITY_TERMS = [
    "inconsistent",
    "contradict",
    "changed testimony",
    "recanted",
    "false",
    "misleading",
]


HIGH_RISK_TERMS = [
    "standing",
    "jurisdiction",
    "prima facie",
    "contract",
    "notice",
]


MEDIUM_RISK_TERMS = [
    "damages",
    "timeline",
    "discovery",
    "causation",
]


LOW_RISK_TERMS = [
    "approximately",
    "possibly",
    "appears to",
]


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def split_sentences(text):
    return re.split(r'(?<=[.!?])\s+', clean_text(text))


def get_doc_text(doc):
    return clean_text(doc.get("text") or doc.get("preview"))


def get_doc_name(doc):
    return clean_text(doc.get("filename") or doc.get("title") or "Unknown Document")


def get_doc_type(doc):
    return clean_text(doc.get("type") or doc.get("category") or "other")


def first_text_document(documents):
    for doc in documents:
        if get_doc_text(doc):
            return doc
    return documents[0] if documents else {}


def find_snippet(text, needle="", limit=420):
    text = clean_text(text)
    needle = clean_text(needle)

    if not text:
        return ""

    if needle:
        lower_text = text.lower()
        lower_needle = needle.lower()
        index = lower_text.find(lower_needle)

        if index >= 0:
            start = max(0, index - 140)
            end = min(len(text), index + len(needle) + 260)
            return clean_text(text[start:end])[:limit]

    return text[:limit]


def find_sentence_with_term(text, term):
    term = clean_text(term).lower()

    for sentence in split_sentences(text):
        if term and term in sentence.lower():
            return sentence[:500]

    return find_snippet(text, term)


def find_document_with_term(documents, term):
    term = clean_text(term).lower()

    for doc in documents:
        text = get_doc_text(doc).lower()
        if term and term in text:
            return doc

    return first_text_document(documents)


def build_issue_object(
    issue,
    category="general",
    source_doc=None,
    source_snippet="",
    supporting_allegation="",
    supporting_source_doc=None,
    risk_level="medium",
    recommended_focus="Review underlying record support.",
    reason="General litigation concern detected.",
):
    source_doc = source_doc or {}
    supporting_source_doc = supporting_source_doc or {}

    scored = calculate_issue_score(issue)

    if scored.get("score", 40) >= 85:
        risk_level = "high"
    elif scored.get("score", 40) >= 65:
        risk_level = "medium"
    else:
        risk_level = risk_level or "low"

    if scored.get("recommended_focus"):
        recommended_focus = scored["recommended_focus"]

    if scored.get("reason"):
        reason = scored["reason"]

    return {
        "issue": clean_text(issue),
        "category": category,
        "score": scored.get("score", 40),
        "risk_level": risk_level,
        "reason": reason,
        "recommended_focus": recommended_focus,
        "source_document": get_doc_name(source_doc),
        "source_type": get_doc_type(source_doc),
        "source_snippet": clean_text(source_snippet),
        "supporting_allegation": clean_text(supporting_allegation),
        "supporting_source_document": get_doc_name(supporting_source_doc) if supporting_source_doc else "",
        "supporting_source_type": get_doc_type(supporting_source_doc) if supporting_source_doc else "",
    }


def detect_motion_type(selected_case, documents):
    selected_motion = clean_text((selected_case or {}).get("motion")).lower()

    if selected_motion:
        return selected_motion

    combined = " ".join(
        clean_text(doc.get("filename", "")).lower()
        for doc in documents
    )

    if "summary judgment" in combined:
        return "summary judgment motion"

    if "dismiss" in combined or "3211" in combined:
        return "motion to dismiss"

    if "default judgment" in combined:
        return "default judgment motion"

    return "general motion"


def classify_documents(documents):
    grouped = {}

    for doc in documents:
        doc_type = doc.get("type", "other")
        grouped.setdefault(doc_type, []).append(doc)

    return grouped


def detect_missing_documents(documents, motion_type):
    existing = set()

    for doc in documents:
        existing.add(doc.get("type", "other"))

    required = REQUIRED_DOCUMENT_TYPES.get(motion_type, [])

    missing = []

    source_doc = first_text_document(documents)

    for item in required:
        if item not in existing:
            missing.append(
                build_issue_object(
                    issue=f"Missing expected document category: {item}.",
                    category="missing_document",
                    source_doc=source_doc,
                    source_snippet="Document inventory does not include this expected filing category.",
                    risk_level="medium",
                    recommended_focus="Confirm whether the matter folder is complete before drafting.",
                    reason=f"Expected {item} for {motion_type}, but none was classified.",
                )
            )

    return missing


def detect_burden_issues(motion_type, documents):
    source_doc = first_text_document(documents)
    rules = BURDEN_RULES.get(motion_type, [])

    issues = []

    for rule in rules:
        issues.append(
            build_issue_object(
                issue=rule,
                category="burden",
                source_doc=source_doc,
                source_snippet=f"Motion type detected: {motion_type}.",
                risk_level="high",
                recommended_focus="Use as threshold briefing framework.",
                reason="Burden rule generated from detected motion posture.",
            )
        )

    return issues


def extract_dates(text):
    found = []

    for pattern in DATE_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)

        for match in matches:
            if match not in found:
                found.append(match)

    return found


def detect_date_contradictions(documents):
    date_map = {}

    for doc in documents:
        text = get_doc_text(doc)[:5000]
        filename = get_doc_name(doc)

        dates = extract_dates(text)

        for item in dates:
            date_map.setdefault(item, []).append(
                {
                    "filename": filename,
                    "doc": doc,
                    "snippet": find_sentence_with_term(text, item),
                }
            )

    contradictions = []

    for date_value, sources in date_map.items():
        unique_names = sorted(set(item["filename"] for item in sources))

        if len(unique_names) >= 3:
            first_source = sources[0]
            contradictions.append(
                build_issue_object(
                    issue=f"Date '{date_value}' appears across multiple documents and should be verified for consistency.",
                    category="date_contradiction",
                    source_doc=first_source["doc"],
                    source_snippet=first_source["snippet"],
                    supporting_allegation=f"Same date appears in: {', '.join(unique_names[:5])}.",
                    supporting_source_doc=first_source["doc"],
                    risk_level="medium",
                    recommended_focus="Compare the record timeline and confirm whether the date is consistent.",
                    reason="Same date appears across multiple document sources.",
                )
            )

    return contradictions[:10]


def extract_allegations(documents):
    allegations = []

    for doc in documents:
        text = get_doc_text(doc)
        filename = get_doc_name(doc)
        doc_type = get_doc_type(doc)

        sentences = split_sentences(text)

        for sentence in sentences:
            lower = sentence.lower()

            for pattern in ALLEGATION_PATTERNS:
                if pattern in lower:
                    allegations.append(
                        {
                            "statement": sentence[:500],
                            "source": filename,
                            "doc_type": doc_type,
                            "source_document": filename,
                            "source_type": doc_type,
                            "source_snippet": sentence[:500],
                            "doc": doc,
                        }
                    )
                    break

    return allegations[:50]


def detect_missing_proof(allegations, documents):
    findings = []

    combined = " ".join(
        get_doc_text(doc).lower()
        for doc in documents
    )

    seen = set()

    for item in allegations:
        statement = item.get("statement", "")
        statement_lower = statement.lower()
        source_doc = item.get("doc") or {}
        source_snippet = item.get("source_snippet") or statement

        if "notice" in statement_lower and "exhibit" not in combined:
            issue = "Notice allegation may lack exhibit support."
            if issue not in seen:
                findings.append(
                    build_issue_object(
                        issue=issue,
                        category="missing_proof",
                        source_doc=source_doc,
                        source_snippet=source_snippet,
                        supporting_allegation=statement,
                        supporting_source_doc=source_doc,
                        risk_level="high",
                        recommended_focus="Demand or identify notice exhibit proof.",
                        reason="Notice allegation detected, but no exhibit reference found across the document set.",
                    )
                )
                seen.add(issue)

        if "damages" in statement_lower and "invoice" not in combined:
            issue = "Damages allegations may lack documentary support."
            if issue not in seen:
                findings.append(
                    build_issue_object(
                        issue=issue,
                        category="missing_proof",
                        source_doc=source_doc,
                        source_snippet=source_snippet,
                        supporting_allegation=statement,
                        supporting_source_doc=source_doc,
                        risk_level="medium",
                        recommended_focus="Look for invoices, ledgers, payment records, or damages exhibits.",
                        reason="Damages allegation detected, but no invoice reference found across the document set.",
                    )
                )
                seen.add(issue)

        if "contract" in statement_lower and "agreement" not in combined:
            issue = "Contract allegations may lack agreement or contract exhibit."
            if issue not in seen:
                findings.append(
                    build_issue_object(
                        issue=issue,
                        category="missing_proof",
                        source_doc=source_doc,
                        source_snippet=source_snippet,
                        supporting_allegation=statement,
                        supporting_source_doc=source_doc,
                        risk_level="high",
                        recommended_focus="Confirm whether the operative agreement is in the record.",
                        reason="Contract allegation detected, but no agreement reference found across the document set.",
                    )
                )
                seen.add(issue)

    return findings[:10]


def detect_position_conflicts(allegations):
    conflicts = []

    complaint_claims = []
    defense_claims = []

    for item in allegations:
        doc_type = item.get("doc_type", "")
        statement = item.get("statement", "")

        if doc_type == "complaint":
            complaint_claims.append(item)

        if doc_type in ["answer", "opposition"]:
            defense_claims.append(item)

    for plaintiff_item in complaint_claims:
        for defense_item in defense_claims:
            plaintiff_statement = plaintiff_item.get("statement", "")
            defense_statement = defense_item.get("statement", "")

            if (
                "breach" in plaintiff_statement.lower()
                and "deny" in defense_statement.lower()
            ):
                conflicts.append(
                    {
                        "issue": "Potential breach contradiction detected.",
                        "plaintiff_position": plaintiff_statement[:220],
                        "defense_position": defense_statement[:220],
                        "risk_level": "high",
                        "source_document": plaintiff_item.get("source_document", ""),
                        "source_type": plaintiff_item.get("source_type", ""),
                        "source_snippet": plaintiff_statement[:500],
                        "supporting_allegation": defense_statement[:500],
                        "supporting_source_document": defense_item.get("source_document", ""),
                        "supporting_source_type": defense_item.get("source_type", ""),
                        "plaintiff_doc": plaintiff_item.get("doc", {}),
                        "defense_doc": defense_item.get("doc", {}),
                    }
                )

    return conflicts[:10]


def position_conflicts_to_issues(position_conflicts):
    issues = []

    for conflict in position_conflicts:
        issues.append(
            build_issue_object(
                issue=f"{conflict['issue']} Risk Level: {conflict['risk_level']}.",
                category="position_conflict",
                source_doc=conflict.get("plaintiff_doc", {}),
                source_snippet=conflict.get("source_snippet", ""),
                supporting_allegation=conflict.get("supporting_allegation", ""),
                supporting_source_doc=conflict.get("defense_doc", {}),
                risk_level=conflict.get("risk_level", "high"),
                recommended_focus="Use conflicting positions to frame disputed facts or credibility attack.",
                reason="Complaint-side allegation appears to conflict with defense-side denial.",
            )
        )

    return issues


def detect_weak_allegations(documents):
    weak = []

    seen = set()

    for doc in documents:
        text = get_doc_text(doc)
        filename = get_doc_name(doc)
        lower = text.lower()

        for phrase in WEAK_PHRASES:
            if phrase in lower:
                key = (filename, phrase)

                if key in seen:
                    continue

                weak.append(
                    build_issue_object(
                        issue=f"Potential weak allegation language detected: '{phrase}'.",
                        category="weak_allegation",
                        source_doc=doc,
                        source_snippet=find_sentence_with_term(text, phrase),
                        supporting_allegation=find_sentence_with_term(text, phrase),
                        supporting_source_doc=doc,
                        risk_level="medium",
                        recommended_focus="Use uncertainty language for impeachment, narrowing, or burden attack.",
                        reason=f"Document contains weak or uncertain phrase: {phrase}.",
                    )
                )
                seen.add(key)

    return weak[:12]


def detect_attack_points(documents):
    findings = []

    combined = " ".join(
        get_doc_text(doc)
        for doc in documents
    ).lower()

    for keyword, message in ATTACK_KEYWORDS.items():
        if keyword in combined:
            source_doc = find_document_with_term(documents, keyword)
            source_text = get_doc_text(source_doc)

            findings.append(
                build_issue_object(
                    issue=message,
                    category="attack_point",
                    source_doc=source_doc,
                    source_snippet=find_sentence_with_term(source_text, keyword),
                    supporting_allegation=find_sentence_with_term(source_text, keyword),
                    supporting_source_doc=source_doc,
                    risk_level="high" if keyword in HIGH_RISK_TERMS else "medium",
                    recommended_focus="Evaluate whether this can become a dispositive or high-value briefing point.",
                    reason=f"Attack keyword detected in record: {keyword}.",
                )
            )

    return findings[:12]


def detect_fact_risks(documents):
    risks = []

    seen = set()

    for doc in documents:
        text = get_doc_text(doc)
        filename = get_doc_name(doc)
        lower = text.lower()

        for term in FACT_RISK_TERMS:
            if term in lower:
                key = (filename, term)

                if key in seen:
                    continue

                risks.append(
                    build_issue_object(
                        issue=f"Potential factual uncertainty detected: '{term}'.",
                        category="fact_risk",
                        source_doc=doc,
                        source_snippet=find_sentence_with_term(text, term),
                        supporting_allegation=find_sentence_with_term(text, term),
                        supporting_source_doc=doc,
                        risk_level="medium",
                        recommended_focus="Clarify the factual record and test whether the uncertainty affects an element.",
                        reason=f"Fact-risk term detected in source document: {term}.",
                    )
                )
                seen.add(key)

    return risks[:10]


def detect_credibility_flags(documents):
    flags = []

    seen = set()

    for doc in documents:
        text = get_doc_text(doc)
        filename = get_doc_name(doc)
        lower = text.lower()

        for term in CREDIBILITY_TERMS:
            if term in lower:
                key = (filename, term)

                if key in seen:
                    continue

                flags.append(
                    build_issue_object(
                        issue=f"Potential credibility issue detected: '{term}'.",
                        category="credibility",
                        source_doc=doc,
                        source_snippet=find_sentence_with_term(text, term),
                        supporting_allegation=find_sentence_with_term(text, term),
                        supporting_source_doc=doc,
                        risk_level="medium",
                        recommended_focus="Use for impeachment, contradiction analysis, or witness credibility attack.",
                        reason=f"Credibility term detected in source document: {term}.",
                    )
                )
                seen.add(key)

    return flags[:10]


def calculate_issue_score(issue_text):
    text = clean_text(issue_text).lower()

    score = 40
    category = "general"
    reason = "General litigation concern detected."
    recommended_focus = "Review underlying record support."

    for term in HIGH_RISK_TERMS:
        if term in text:
            score = 90
            category = "dispositive"
            reason = f"Issue contains potentially dispositive term: {term}."
            recommended_focus = "Attack immediately and prioritize in briefing."
            return {
                "issue": issue_text,
                "score": score,
                "category": category,
                "reason": reason,
                "recommended_focus": recommended_focus,
            }

    for term in MEDIUM_RISK_TERMS:
        if term in text:
            score = 70
            category = "material"
            reason = f"Issue contains materially significant term: {term}."
            recommended_focus = "Develop factual and evidentiary attack."
            return {
                "issue": issue_text,
                "score": score,
                "category": category,
                "reason": reason,
                "recommended_focus": recommended_focus,
            }

    for term in LOW_RISK_TERMS:
        if term in text:
            score = 50
            category = "credibility"
            reason = f"Issue contains uncertainty term: {term}."
            recommended_focus = "Use for impeachment or credibility attack."
            return {
                "issue": issue_text,
                "score": score,
                "category": category,
                "reason": reason,
                "recommended_focus": recommended_focus,
            }

    return {
        "issue": issue_text,
        "score": score,
        "category": category,
        "reason": reason,
        "recommended_focus": recommended_focus,
    }


def dedupe_issue_objects(issue_objects):
    unique = []
    seen = set()

    for item in issue_objects:
        key = (
            clean_text(item.get("issue")),
            clean_text(item.get("source_document")),
            clean_text(item.get("source_snippet"))[:120],
        )

        if key in seen:
            continue

        unique.append(item)
        seen.add(key)

    return unique


def sort_issue_objects(issue_objects):
    sorted_items = list(issue_objects)

    sorted_items.sort(
        key=lambda x: (
            x.get("score", 0),
            1 if x.get("risk_level") == "high" else 0,
            clean_text(x.get("category")),
        ),
        reverse=True,
    )

    return sorted_items


def rank_priority_issues(scored_issues):
    ranked = []

    for item in scored_issues[:8]:
        ranked.append(
            {
                "label": f"[{item.get('score', 0)}] {item.get('issue', '')}",
                "issue": item.get("issue", ""),
                "score": item.get("score", 0),
                "category": item.get("category", ""),
                "source_document": item.get("source_document", ""),
                "source_snippet": item.get("source_snippet", ""),
                "recommended_focus": item.get("recommended_focus", ""),
            }
        )

    return ranked


def flatten_issue_labels(issue_objects):
    return [item.get("issue", "") for item in issue_objects if item.get("issue")]


def build_issue_analysis(selected_case, documents=None, attorney_notes=None):
    """
    Core litigation issue detection engine.
    v3.4 adds deterministic source traceability.
    """

    documents = documents or []
    attorney_notes = attorney_notes or []

    motion_type = detect_motion_type(selected_case, documents)

    document_groups = classify_documents(documents)

    missing_evidence = detect_missing_documents(documents, motion_type)
    burden_issues = detect_burden_issues(motion_type, documents)
    contradictions = detect_date_contradictions(documents)

    allegations = extract_allegations(documents)

    position_conflicts = detect_position_conflicts(allegations)
    position_conflict_issues = position_conflicts_to_issues(position_conflicts)

    missing_proof = detect_missing_proof(
        allegations,
        documents,
    )

    weak_claims = detect_weak_allegations(documents)
    attack_points = detect_attack_points(documents)
    fact_risk_flags = detect_fact_risks(documents)
    credibility_flags = detect_credibility_flags(documents)

    core_issues = []
    core_issues.extend(burden_issues)
    core_issues.extend(missing_evidence)
    core_issues.extend(missing_proof)

    all_issues = []
    all_issues.extend(core_issues)
    all_issues.extend(contradictions)
    all_issues.extend(position_conflict_issues)
    all_issues.extend(attack_points)
    all_issues.extend(weak_claims)
    all_issues.extend(fact_risk_flags)
    all_issues.extend(credibility_flags)

    all_issues = sort_issue_objects(dedupe_issue_objects(all_issues))

    priority_ranking = rank_priority_issues(
        all_issues,
    )

    attorney_focus = []

    for item in all_issues[:5]:
        focus = item.get("recommended_focus")
        if focus and focus not in attorney_focus:
            attorney_focus.append(focus)

    return {
        "engine": ENGINE_VERSION,
        "motion_type": motion_type,
        "document_groups": document_groups,
        "core_issues": core_issues,
        "core_issue_labels": flatten_issue_labels(core_issues),
        "contradictions": contradictions + position_conflict_issues,
        "contradiction_labels": flatten_issue_labels(contradictions + position_conflict_issues),
        "attack_points": attack_points,
        "attack_point_labels": flatten_issue_labels(attack_points),
        "missing_evidence": missing_evidence,
        "missing_evidence_labels": flatten_issue_labels(missing_evidence),
        "missing_proof": missing_proof,
        "missing_proof_labels": flatten_issue_labels(missing_proof),
        "weak_claims": weak_claims,
        "weak_claim_labels": flatten_issue_labels(weak_claims),
        "priority_ranking": priority_ranking,
        "position_conflicts": position_conflicts,
        "allegations": allegations,
        "scored_issues": all_issues,
        "all_issues": all_issues,
        "attorney_focus": attorney_focus,
        "attorney_notes": attorney_notes,
        "fact_risk_flags": fact_risk_flags,
        "fact_risk_labels": flatten_issue_labels(fact_risk_flags),
        "credibility_flags": credibility_flags,
        "credibility_flag_labels": flatten_issue_labels(credibility_flags),
    }
