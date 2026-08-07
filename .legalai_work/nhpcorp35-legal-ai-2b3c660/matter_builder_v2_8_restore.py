# matter_builder.py
from pathlib import Path
import re

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from docx import Document
except Exception:
    Document = None


DEFAULT_MATTER_FOLDER = Path("matter_docs")


DOCUMENT_GROUPS = {
    "selected_case": "Selected Case",
    "complaint": "Complaint",
    "answer": "Answer",
    "motion": "Motions",
    "affirmation": "Affirmations",
    "opposition": "Oppositions",
    "reply": "Replies",
    "exhibit": "Exhibits",
    "memo": "Memoranda of Law",
    "order": "Orders / Decisions",
    "other": "Other Documents",
}


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".rtf",
}


SKIP_FOLDERS = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
}


COURT_HEADER_WORDS = {
    "supreme court",
    "civil court",
    "county court",
    "surrogate",
    "appellate division",
    "state of new york",
    "united states",
}


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def clean_case_party(value):
    value = clean_text(value)

    value = re.sub(r"(?i)\bSUPREME COURT OF THE STATE OF NEW YORK\b", "", value)
    value = re.sub(r"(?i)\bSTATE OF NEW YORK\b", "", value)
    value = re.sub(r"(?i)\bCOUNTY OF [A-Z\s]+\b", "", value)
    value = re.sub(r"(?i)\bINDEX\s*(NO\.?|NUMBER)?\s*[:#]?\s*[0-9]{4,8}/?[0-9]{0,4}\b", "", value)
    value = re.sub(r"(?i)\bPlaintiff[s]?\b", "", value)
    value = re.sub(r"(?i)\bDefendant[s]?\b", "", value)
    value = re.sub(r"(?i)\bPetitioner[s]?\b", "", value)
    value = re.sub(r"(?i)\bRespondent[s]?\b", "", value)

    value = value.replace(" -against- ", " ")
    value = value.replace(" against ", " ")

    value = re.sub(r"[_|]+", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip(" ,.-")


def classify_by_filename(filename):
    name = filename.lower()

    if "selected case" in name or "search result" in name:
        return "selected_case"

    if "complaint" in name:
        return "complaint"

    if "answer" in name:
        return "answer"

    if "notice of motion" in name or "motion" in name:
        return "motion"

    if "affirmation" in name or "affidavit" in name or "declaration" in name:
        return "affirmation"

    if "opposition" in name or "opp" in name:
        return "opposition"

    if "reply" in name:
        return "reply"

    if "exhibit" in name or "exh" in name:
        return "exhibit"

    if "memo" in name or "memorandum" in name or "memorandum of law" in name:
        return "memo"

    if "order" in name or "decision" in name or "judgment" in name:
        return "order"

    return "other"


def extract_txt(path):
    try:
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def extract_pdf(path):
    if PdfReader is None:
        return ""

    try:
        reader = PdfReader(str(path))
        pages = []

        for page in reader.pages[:20]:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)

        return "\n".join(pages)

    except Exception:
        return ""


def extract_docx(path):
    if Document is None:
        return ""

    try:
        doc = Document(str(path))
        paragraphs = []

        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            if text:
                paragraphs.append(text)

        return "\n".join(paragraphs)

    except Exception:
        return ""


def extract_text(path):
    suffix = path.suffix.lower()

    if suffix == ".txt":
        return extract_txt(path)

    if suffix == ".pdf":
        return extract_pdf(path)

    if suffix == ".docx":
        return extract_docx(path)

    return ""


def should_skip_path(path):
    parts = set(path.parts)

    for folder in SKIP_FOLDERS:
        if folder in parts:
            return True

    if path.name.startswith("."):
        return True

    return False


def find_matter_files(folder_path):
    folder = Path(folder_path)

    if not folder.exists() or not folder.is_dir():
        return []

    files = []

    for path in folder.rglob("*"):
        if should_skip_path(path):
            continue

        if not path.is_file():
            continue

        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue

        files.append(path)

    return sorted(files, key=lambda p: str(p).lower())


def read_matter_folder(folder_path=DEFAULT_MATTER_FOLDER):
    folder = Path(folder_path)
    files = find_matter_files(folder)

    documents = []

    for path in files:
        doc_type = classify_by_filename(path.name)
        extracted_text = clean_text(extract_text(path))

        documents.append(
            {
                "filename": path.name,
                "title": path.name,
                "path": str(path),
                "relative_path": str(path.relative_to(folder)) if folder.exists() else str(path),
                "folder": str(path.parent),
                "type": doc_type,
                "category": doc_type,
                "group": DOCUMENT_GROUPS.get(doc_type, DOCUMENT_GROUPS["other"]),
                "text": extracted_text,
                "preview": extracted_text[:800],
                "source": "folder",
            }
        )

    return documents


def selected_case_to_document(selected_case):
    if not selected_case:
        return None

    title = clean_text(selected_case.get("title") or selected_case.get("case_name") or "Selected Case")
    court = clean_text(selected_case.get("court"))
    date = clean_text(selected_case.get("date"))
    citation = clean_text(selected_case.get("citation"))
    outcome = clean_text(selected_case.get("outcome"))
    motion = clean_text(selected_case.get("motion"))
    cause = clean_text(selected_case.get("primary_cause"))
    holding = clean_text(selected_case.get("holding"))
    rule = clean_text(selected_case.get("rule"))
    text = clean_text(
        selected_case.get("formatted_text")
        or selected_case.get("text")
        or selected_case.get("summary")
        or selected_case.get("snippet")
        or ""
    )

    metadata_lines = []

    if title:
        metadata_lines.append(title)
    if court:
        metadata_lines.append(f"Court: {court}")
    if date:
        metadata_lines.append(f"Date: {date}")
    if citation:
        metadata_lines.append(f"Citation: {citation}")
    if motion:
        metadata_lines.append(f"Motion: {motion}")
    if outcome:
        metadata_lines.append(f"Outcome: {outcome}")
    if cause:
        metadata_lines.append(f"Cause: {cause}")
    if rule:
        metadata_lines.append(f"Rule: {rule}")
    if holding:
        metadata_lines.append(f"Holding: {holding}")

    combined = clean_text("\n".join(metadata_lines + [text]))

    return {
        "filename": f"Selected Case - {title}",
        "title": title,
        "path": "",
        "relative_path": "Selected from search results",
        "folder": "",
        "type": "selected_case",
        "category": "selected_case",
        "group": DOCUMENT_GROUPS["selected_case"],
        "text": combined,
        "preview": combined[:800],
        "source": "selected_case",
        "court": court,
        "date": date,
        "citation": citation,
        "motion": motion,
        "outcome": outcome,
        "primary_cause": cause,
        "holding": holding,
        "rule": rule,
        "case_id": clean_text(selected_case.get("case_id")),
    }


def normalize_document(document):
    filename = clean_text(
        document.get("filename")
        or document.get("name")
        or document.get("title")
        or "Untitled Document"
    )

    doc_type = (
        document.get("type")
        or document.get("category")
        or classify_by_filename(filename)
    )

    group = DOCUMENT_GROUPS.get(doc_type, DOCUMENT_GROUPS["other"])
    text = clean_text(document.get("text", ""))

    return {
        "filename": filename,
        "title": clean_text(document.get("title") or filename),
        "path": document.get("path", ""),
        "relative_path": document.get("relative_path", document.get("path", "")),
        "folder": document.get("folder", ""),
        "type": doc_type,
        "category": doc_type,
        "group": group,
        "text": text,
        "preview": text[:800],
        "source": document.get("source", "manual"),
        "court": document.get("court", ""),
        "date": document.get("date", ""),
        "citation": document.get("citation", ""),
        "motion": document.get("motion", ""),
        "outcome": document.get("outcome", ""),
        "primary_cause": document.get("primary_cause", ""),
        "holding": document.get("holding", ""),
        "rule": document.get("rule", ""),
        "case_id": document.get("case_id", ""),
    }


def group_documents(documents):
    grouped = {label: [] for label in DOCUMENT_GROUPS.values()}

    for document in documents:
        normalized = normalize_document(document)
        grouped[normalized["group"]].append(normalized)

    return grouped


def combined_text(documents, limit=50000):
    chunks = []

    for doc in documents:
        text = clean_text(doc.get("text", ""))
        if text:
            chunks.append(text)

    return clean_text(" ".join(chunks))[:limit]


def get_text_lines(text):
    raw_lines = re.split(r"[\r\n]+", str(text or ""))
    lines = []

    for line in raw_lines:
        cleaned = clean_text(line)
        if cleaned:
            lines.append(cleaned)

    return lines


def is_court_header_line(line):
    lower = line.lower()
    return any(word in lower for word in COURT_HEADER_WORDS)


def extract_index_number(text):
    patterns = [
        r"index\s*(?:no\.?|number)?\s*[:#]?\s*([0-9]{4,8}/[0-9]{4})",
        r"index\s*(?:no\.?|number)?\s*[:#]?\s*([0-9]{5,8})",
        r"idx\s*(?:no\.?)?\s*[:#]?\s*([0-9]{4,8}/[0-9]{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    return "—"


def extract_case_name_from_lines(text):
    lines = get_text_lines(text)

    for line in lines[:80]:
        if is_court_header_line(line):
            continue

        if re.search(r"\bv\.?\b", line, re.IGNORECASE):
            match = re.search(
                r"(.{2,120}?)\s+\bv\.?\b\s+(.{2,120})",
                line,
                re.IGNORECASE,
            )

            if match:
                left = clean_case_party(match.group(1))
                right = clean_case_party(match.group(2))

                if left and right:
                    return f"{left} v. {right}"

        if re.search(r"(?i)-against-| against ", line):
            parts = re.split(r"(?i)-against-| against ", line, maxsplit=1)
            if len(parts) == 2:
                left = clean_case_party(parts[0])
                right = clean_case_party(parts[1])

                if left and right:
                    return f"{left} v. {right}"

    return ""


def extract_case_name(text):
    line_case_name = extract_case_name_from_lines(text)
    if line_case_name:
        return line_case_name

    cleaned = re.sub(
        r"(?i)SUPREME COURT OF THE STATE OF NEW YORK|STATE OF NEW YORK|COUNTY OF [A-Z\s]+",
        " ",
        text,
    )

    patterns = [
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,80})\s+v\.?\s+([A-Z][A-Za-z0-9&.,'\-\s]{2,80})",
        r"([A-Z][A-Za-z0-9&.,'\-\s]{2,80})\s+against\s+([A-Z][A-Za-z0-9&.,'\-\s]{2,80})",
    ]

    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            left = clean_case_party(match.group(1))
            right = clean_case_party(match.group(2))

            if left and right:
                return f"{left} v. {right}"

    return "Matter Builder"


def extract_parties(case_name):
    if " v. " not in case_name:
        return {"plaintiff": "—", "defendant": "—"}

    left, right = case_name.split(" v. ", 1)

    return {
        "plaintiff": clean_case_party(left) or "—",
        "defendant": clean_case_party(right) or "—",
    }


def detect_motion_posture(documents, text):
    selected_case_docs = [doc for doc in documents if doc.get("type") == "selected_case"]

    for doc in selected_case_docs:
        motion = clean_text(doc.get("motion"))
        if motion:
            return motion.title()

    names = " ".join(doc.get("filename", "") for doc in documents).lower()
    haystack = f"{names} {text.lower()}"

    if "summary judgment" in haystack:
        return "Summary judgment motion"

    if "dismiss" in haystack or "3211" in haystack:
        return "Motion to dismiss"

    if "default judgment" in haystack:
        return "Default judgment motion"

    if "discovery" in haystack or "compel" in haystack:
        return "Discovery motion"

    if "opposition" in haystack:
        return "Opposition papers"

    return "—"


def detect_procedural_posture(text):
    lower = text.lower()

    if "selected case" in lower and "complaint" in lower and "motion" in lower:
        return "Selected case and matter folder documents are present."

    if "complaint" in lower and "answer" in lower and "motion" in lower:
        return "Pleadings and motion papers are present."

    if "complaint" in lower and "motion" in lower:
        return "Complaint and motion papers are present."

    if "answer" in lower and "counterclaim" in lower:
        return "Answer with counterclaims appears to be present."

    if "order" in lower or "decision" in lower:
        return "Prior order or decision appears to be present."

    return "Procedural posture not yet detected from extracted text."


def strongest_motion_documents(documents):
    ranked = []

    weights = {
        "selected_case": 120,
        "motion": 100,
        "opposition": 90,
        "affirmation": 80,
        "memo": 75,
        "reply": 70,
        "order": 60,
        "complaint": 40,
        "answer": 35,
        "exhibit": 20,
        "other": 10,
    }

    for doc in documents:
        doc_type = doc.get("type", "other")
        score = weights.get(doc_type, 0)

        filename = doc.get("filename", "").lower()

        if "summary judgment" in filename:
            score += 25
        if "motion" in filename:
            score += 15
        if "opposition" in filename:
            score += 15
        if "memo" in filename or "memorandum" in filename:
            score += 10

        ranked.append(
            {
                "filename": doc.get("filename", ""),
                "type": doc_type,
                "group": doc.get("group", ""),
                "score": score,
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:5]


def selected_case_summary(documents):
    for doc in documents:
        if doc.get("type") == "selected_case":
            return {
                "title": doc.get("title", ""),
                "court": doc.get("court", ""),
                "date": doc.get("date", ""),
                "citation": doc.get("citation", ""),
                "motion": doc.get("motion", ""),
                "outcome": doc.get("outcome", ""),
                "primary_cause": doc.get("primary_cause", ""),
                "holding": doc.get("holding", ""),
                "rule": doc.get("rule", ""),
                "case_id": doc.get("case_id", ""),
            }

    return None


def document_type_counts(documents):
    counts = {}

    for doc in documents:
        doc_type = doc.get("type") or "other"
        counts[doc_type] = counts.get(doc_type, 0) + 1

    return counts


def docs_by_type(documents, doc_type):
    return [doc for doc in documents if doc.get("type") == doc_type]


def first_available_preview(documents, preferred_types=None, limit=220):
    preferred_types = preferred_types or []

    ordered_docs = []

    for doc_type in preferred_types:
        ordered_docs.extend(docs_by_type(documents, doc_type))

    for doc in documents:
        if doc not in ordered_docs:
            ordered_docs.append(doc)

    for doc in ordered_docs:
        preview = clean_text(doc.get("preview") or doc.get("text"))
        if preview:
            return preview[:limit]

    return ""


def has_any_document_type(documents, types):
    return any(doc.get("type") in types for doc in documents)



def extract_nyscef_doc_no(document):
    text = clean_text(document.get("text") or document.get("preview") or "")
    filename = clean_text(document.get("filename"))
    haystack = f"{filename} {text[:2000]}"

    patterns = [
        r"NYSCEF\s*(?:Doc\.?|Document)?\s*(?:No\.?|Number)?\s*[:#]?\s*(\d+)",
        r"Doc\.?\s*No\.?\s*[:#]?\s*(\d+)",
        r"Document\s+No\.?\s*[:#]?\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, haystack, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    return "__"


def normalize_exhibit_label(value):
    value = clean_text(value).upper()
    value = value.replace("EXHIBIT", "").replace("EXH.", "").replace("EXH", "")
    value = re.sub(r"[^A-Z0-9]", "", value)
    if value:
        return value[:8]
    return "__"


def detect_exhibit_label(document, fallback_index=1):
    filename = clean_text(document.get("filename"))
    text = clean_text(document.get("text") or document.get("preview") or "")
    haystack = f"{filename} {text[:1200]}"

    patterns = [
        r"Exhibit\s+([A-Z0-9]{1,4})",
        r"Exh\.?\s+([A-Z0-9]{1,4})",
        r"Ex\.\s+([A-Z0-9]{1,4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, haystack, re.IGNORECASE)
        if match:
            return normalize_exhibit_label(match.group(1))

    if document.get("type") == "exhibit":
        return chr(64 + fallback_index) if 1 <= fallback_index <= 26 else str(fallback_index)

    return "__"


def citation_text_for_document(document, exhibit_label=None):
    doc_no = extract_nyscef_doc_no(document)
    label = exhibit_label or detect_exhibit_label(document)

    if label and label != "__":
        return f"See Exhibit {label}; NYSCEF Doc. No. {doc_no}."

    return f"See NYSCEF Doc. No. {doc_no}."


def split_support_sentences(text, limit=18):
    cleaned = clean_text(text)
    if not cleaned:
        return []

    rough_sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    sentences = []

    for sentence in rough_sentences:
        sentence = clean_text(sentence)
        if len(sentence) < 45:
            continue
        if len(sentence) > 360:
            sentence = sentence[:357].rstrip() + "..."
        sentences.append(sentence)
        if len(sentences) >= limit:
            break

    return sentences


def support_keywords_for_sentence(sentence):
    words = re.findall(r"[A-Za-z][A-Za-z0-9'\-]{3,}", sentence.lower())
    stop_words = {
        "that", "this", "with", "from", "were", "have", "been", "will", "would", "could",
        "should", "there", "their", "where", "which", "when", "what", "into", "upon",
        "here", "court", "plaintiff", "defendant", "motion", "matter", "record",
    }
    unique = []
    for word in words:
        if word in stop_words:
            continue
        if word not in unique:
            unique.append(word)
    return unique[:8]


def rank_supporting_documents(sentence, documents, max_results=3):
    keywords = support_keywords_for_sentence(sentence)
    ranked = []

    for doc in documents:
        text = clean_text(doc.get("text") or doc.get("preview") or "").lower()
        if not text:
            continue

        score = 0
        for keyword in keywords:
            if keyword in text:
                score += 5

        doc_type = doc.get("type") or "other"
        if doc_type in {"exhibit", "affirmation", "opposition", "motion", "complaint", "answer"}:
            score += 4
        if doc_type in {"selected_case", "memo", "order"}:
            score += 1

        if score > 0:
            ranked.append(
                {
                    "filename": doc.get("filename", ""),
                    "type": doc_type,
                    "group": doc.get("group", ""),
                    "score": score,
                    "citation": citation_text_for_document(doc),
                }
            )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:max_results]


def build_exhibit_inventory(documents):
    inventory = []
    exhibit_docs = [doc for doc in documents if doc.get("type") == "exhibit"]

    for index, doc in enumerate(exhibit_docs, start=1):
        label = detect_exhibit_label(doc, index)
        preview = clean_text(doc.get("preview") or doc.get("text"))[:260]
        inventory.append(
            {
                "label": label,
                "filename": doc.get("filename", ""),
                "doc_no": extract_nyscef_doc_no(doc),
                "citation": citation_text_for_document(doc, label),
                "preview": preview,
            }
        )

    return inventory


def build_affidavit_support(documents):
    support = []
    affidavit_docs = [doc for doc in documents if doc.get("type") == "affirmation"]

    for doc in affidavit_docs[:8]:
        sentences = split_support_sentences(doc.get("text") or doc.get("preview"), limit=4)
        support.append(
            {
                "filename": doc.get("filename", ""),
                "doc_no": extract_nyscef_doc_no(doc),
                "citation": citation_text_for_document(doc),
                "support_points": sentences or ["Review this affirmation or affidavit for record-supported factual assertions."],
            }
        )

    return support


def candidate_fact_sentences(documents):
    preferred = ["complaint", "answer", "motion", "opposition", "affirmation", "exhibit"]
    facts = []

    for doc_type in preferred:
        for doc in documents:
            if doc.get("type") != doc_type:
                continue
            for sentence in split_support_sentences(doc.get("text") or doc.get("preview"), limit=6):
                if re.search(r"\b(argue|contend|therefore|wherefore|respectfully|pursuant)\b", sentence, re.IGNORECASE):
                    continue
                facts.append(
                    {
                        "fact": sentence,
                        "source_document": doc.get("filename", ""),
                        "source_type": doc.get("type", ""),
                        "suggested_citation": citation_text_for_document(doc),
                        "supporting_documents": rank_supporting_documents(sentence, documents, max_results=3),
                    }
                )
                if len(facts) >= 10:
                    return facts

    return facts


def build_contradiction_support_links(summary, documents):
    weaknesses = build_weaknesses(summary, documents)
    links = []

    for weakness in weaknesses:
        supporting = rank_supporting_documents(weakness, documents, max_results=3)
        links.append(
            {
                "weakness": weakness,
                "supporting_documents": supporting,
                "note": "Use these documents to confirm, narrow, or contradict the weakness before finalizing the draft.",
            }
        )

    return links


def build_citation_exhibit_engine(summary, documents):
    fact_map = candidate_fact_sentences(documents)
    exhibit_inventory = build_exhibit_inventory(documents)
    affidavit_support = build_affidavit_support(documents)
    contradiction_links = build_contradiction_support_links(summary, documents)

    citation_suggestions = []
    for fact in fact_map[:6]:
        citation_suggestions.append(
            {
                "draft_fact": fact.get("fact"),
                "suggested_citation": fact.get("suggested_citation"),
                "source_document": fact.get("source_document"),
            }
        )

    if not citation_suggestions and exhibit_inventory:
        for exhibit in exhibit_inventory[:6]:
            citation_suggestions.append(
                {
                    "draft_fact": "Use this exhibit as record support after attorney review.",
                    "suggested_citation": exhibit.get("citation"),
                    "source_document": exhibit.get("filename"),
                }
            )

    return {
        "exhibit_inventory": exhibit_inventory,
        "affidavit_support": affidavit_support,
        "fact_support_map": fact_map,
        "contradiction_support_links": contradiction_links,
        "citation_suggestions": citation_suggestions,
        "drafting_note": "Attorney must verify exhibit labels, NYSCEF numbers, paragraph numbers, page pins, and admissibility before filing.",
    }

def build_authority_list(summary, documents):
    selected_case = summary.get("selected_case") or {}
    strongest_docs = summary.get("strongest_motion_documents", [])
    authorities = []

    if selected_case.get("title"):
        rule = clean_text(selected_case.get("rule"))
        authority = {
            "title": selected_case.get("title"),
            "source": "Selected Case",
            "detail": rule or selected_case.get("holding") or "Selected from search results.",
        }
        authorities.append(authority)

    for doc in strongest_docs[:4]:
        filename = clean_text(doc.get("filename"))
        group = clean_text(doc.get("group"))
        if filename:
            authorities.append(
                {
                    "title": filename,
                    "source": group or "Matter Document",
                    "detail": f"Ranked as a strong matter document for attorney review. Score: {doc.get('score', 0)}.",
                }
            )

    if not authorities:
        authorities.append(
            {
                "title": "No authority identified yet",
                "source": "Matter Builder",
                "detail": "Add motion papers, opposition papers, memoranda, orders, or selected cases to strengthen this section.",
            }
        )

    return authorities


def build_plaintiff_arguments(summary, documents):
    motion = clean_text(summary.get("motion_posture")).lower()
    counts = document_type_counts(documents)
    arguments = []

    if "summary judgment" in motion:
        arguments.extend(
            [
                "Plaintiff is likely arguing entitlement to judgment as a matter of law.",
                "Plaintiff will try to show that the record contains no triable issue of fact.",
                "Plaintiff will rely on motion papers, affirmations, exhibits, and selected authority to shift the burden to the opposition.",
            ]
        )
    elif "dismiss" in motion:
        arguments.extend(
            [
                "Movant is likely attacking the legal sufficiency of the pleading.",
                "Movant may argue that required elements are missing or that claims are duplicative.",
                "Movant may rely on documentary evidence or procedural defects to seek dismissal.",
            ]
        )
    elif "opposition" in motion:
        arguments.extend(
            [
                "The opposing party is likely focused on preserving factual disputes and defeating the requested relief.",
                "The opposition should emphasize burden allocation, admissibility, and gaps in the moving papers.",
            ]
        )
    else:
        arguments.extend(
            [
                "The core moving argument should be identified from the notice of motion, memorandum, and supporting affirmation.",
                "The first legal issue is burden allocation: who must prove what, and at what procedural stage.",
            ]
        )

    if counts.get("complaint"):
        arguments.append("The complaint supplies the pleaded claims and factual theory that the motion must confront.")

    if counts.get("motion"):
        arguments.append("The motion papers likely define the relief requested and the movant's legal theory.")

    if counts.get("memo"):
        arguments.append("The memorandum of law likely contains the movant's most organized legal argument.")

    return arguments[:6]


def build_defense_arguments(summary, documents):
    motion = clean_text(summary.get("motion_posture")).lower()
    selected_case = summary.get("selected_case") or {}
    rule = clean_text(selected_case.get("rule"))
    arguments = []

    if "summary judgment" in motion:
        arguments.extend(
            [
                "Attack the movant's prima facie showing before reaching the sufficiency of opposition proof.",
                "Identify triable issues of fact that require denial.",
                "Challenge admissibility, foundation, personal knowledge, and completeness of the movant's proof.",
                "Argue that credibility disputes cannot be resolved on summary judgment.",
            ]
        )
    elif "dismiss" in motion:
        arguments.extend(
            [
                "Argue that the pleading must be liberally construed and accepted as true at this stage.",
                "Show that the complaint alleges each required element or that factual development is needed.",
                "Distinguish duplicative claims only where necessary and preserve viable alternative theories.",
                "Argue that documentary evidence does not conclusively dispose of the claim.",
            ]
        )
    else:
        arguments.extend(
            [
                "Lead with the burden of proof and procedural standard.",
                "Use missing documents, incomplete proof, and factual disputes to resist the requested relief.",
                "Frame the selected case as the court's rule for what proof matters.",
            ]
        )

    if rule:
        arguments.insert(0, f"Use the selected rule as the anchor: {rule}")

    if has_any_document_type(documents, ["opposition", "affirmation", "memo"]):
        arguments.append("Use opposition papers, affirmations, and memoranda to build a fact-supported attorney argument.")

    return arguments[:7]


def build_weaknesses(summary, documents):
    motion = clean_text(summary.get("motion_posture")).lower()
    selected_case = summary.get("selected_case") or {}
    holding = clean_text(selected_case.get("holding"))
    counts = document_type_counts(documents)
    weaknesses = []

    if "summary judgment" in motion:
        weaknesses.extend(
            [
                "Movant may have failed to establish prima facie entitlement to judgment.",
                "The record may contain triable issues of fact.",
                "Affidavits may lack foundation, personal knowledge, or admissible support.",
                "Causation, damages, notice, control, or contractual breach proof may be incomplete.",
            ]
        )
    elif "dismiss" in motion:
        weaknesses.extend(
            [
                "The motion may improperly contest facts instead of testing pleading sufficiency.",
                "Documentary evidence may not conclusively defeat the allegations.",
                "Dismissal may be premature if facts require discovery.",
                "Duplicative or equitable claims should be narrowed carefully rather than overconceded.",
            ]
        )
    else:
        weaknesses.extend(
            [
                "Procedural posture is not fully detected from available documents.",
                "The matter may be missing key motion papers or opposition papers.",
                "The strongest legal theory may depend on documents not yet ingested.",
            ]
        )

    if not counts.get("motion"):
        weaknesses.append("No motion document detected; the exact requested relief may be incomplete.")

    if not counts.get("opposition"):
        weaknesses.append("No opposition document detected; responsive arguments may need to be drafted from scratch.")

    if not counts.get("memo"):
        weaknesses.append("No memorandum of law detected; legal argument structure may be underdeveloped.")

    if holding:
        weaknesses.append("Compare the current record against the selected case holding to identify missing proof or distinguishing facts.")

    return weaknesses[:8]


def build_drafting_strategy(summary, documents):
    motion = clean_text(summary.get("motion_posture"))
    selected_case = summary.get("selected_case") or {}
    rule = clean_text(selected_case.get("rule"))
    strategy = []

    if rule:
        strategy.append(f"Anchor the opening argument to the selected rule: {rule}")

    if motion and motion != "—":
        strategy.append(f"Frame the brief around the detected posture: {motion}.")

    strategy.extend(
        [
            "Start with the legal standard and burden allocation.",
            "Then attack the moving party's proof before making alternative factual arguments.",
            "Use the strongest matter documents as record support.",
            "Keep the selected case visible as the rule-and-holding comparison point.",
        ]
    )

    preview = first_available_preview(documents, ["motion", "opposition", "memo", "affirmation"])
    if preview:
        strategy.append("Use the extracted document previews to pull record-specific facts into the draft.")

    return strategy[:7]


def build_recommended_outline(summary, documents):
    motion = clean_text(summary.get("motion_posture")).lower()

    if "summary judgment" in motion:
        return [
            "I. Preliminary Statement",
            "II. Procedural Background and Relevant Record",
            "III. Plaintiff/Movant Failed to Establish Prima Facie Entitlement to Judgment",
            "IV. Triable Issues of Fact Require Denial",
            "V. The Moving Proof Is Incomplete, Inadmissible, or Contradicted",
            "VI. The Selected Authority Supports Denial or Narrowing of Relief",
            "VII. Conclusion",
        ]

    if "dismiss" in motion:
        return [
            "I. Preliminary Statement",
            "II. Procedural Background",
            "III. Governing Motion to Dismiss Standard",
            "IV. The Pleading States Viable Claims",
            "V. Documentary Evidence Does Not Conclusively Defeat the Claims",
            "VI. Dismissal Is Premature or Should Be Limited",
            "VII. Conclusion",
        ]

    return [
        "I. Preliminary Statement",
        "II. Procedural Background",
        "III. Governing Legal Standard",
        "IV. Record-Based Argument",
        "V. Authority and Case Comparison",
        "VI. Relief Requested",
        "VII. Conclusion",
    ]


def best_doc_title(documents, types):
    for doc_type in types:
        for doc in documents:
            if doc.get("type") == doc_type:
                return clean_text(doc.get("filename"))
    return ""


def draft_party_label(summary, role):
    value = clean_text(summary.get(role))
    if value and value != "—":
        return value
    if role == "plaintiff":
        return "Plaintiff"
    return "Defendant"


def build_preliminary_statement(summary, documents):
    motion = clean_text(summary.get("motion_posture"))
    case_name = clean_text(summary.get("case_name")) or "this matter"
    plaintiff = draft_party_label(summary, "plaintiff")
    defendant = draft_party_label(summary, "defendant")
    selected_case = summary.get("selected_case") or {}
    rule = clean_text(selected_case.get("rule"))
    holding = clean_text(selected_case.get("holding"))

    if not motion or motion == "—":
        motion = "the pending motion"

    paragraph_one = (
        f"{defendant} respectfully submits this opposition to {motion.lower()} in {case_name}. "
        f"The motion should be denied because the moving papers do not establish entitlement to the relief requested, "
        f"and the available record supports material factual and legal disputes requiring denial or narrowing of the motion."
    )

    paragraph_two = (
        f"The record should be reviewed in light of the parties' actual proof, the procedural posture, "
        f"and the governing burden applicable to {motion.lower()}."
    )

    if rule:
        paragraph_two += f" The selected authority provides the controlling drafting anchor: {rule}"

    paragraph_three = (
        f"At minimum, {plaintiff} has not eliminated the factual and legal issues identified in the opposition record. "
        f"Accordingly, the Court should deny the motion in its entirety, or grant only such limited relief as is supported by admissible proof."
    )

    if holding:
        paragraph_three += f" The selected holding further supports careful comparison between the movant's proof and the actual record before the Court."

    return "\n\n".join([paragraph_one, paragraph_two, paragraph_three])


def build_statement_of_facts(summary, documents):
    case_name = clean_text(summary.get("case_name")) or "this matter"
    motion_doc = best_doc_title(documents, ["motion", "memo", "affirmation"])
    opposition_doc = best_doc_title(documents, ["opposition", "affirmation", "memo"])
    complaint_doc = best_doc_title(documents, ["complaint"])
    answer_doc = best_doc_title(documents, ["answer"])
    order_doc = best_doc_title(documents, ["order"])

    lines = [
        f"This matter, {case_name}, arises from the claims and defenses reflected in the pleadings and motion record.",
    ]

    if complaint_doc:
        lines.append(f"The pleadings include {complaint_doc}, which supplies the factual allegations and claims at issue.")

    if answer_doc:
        lines.append(f"The record also includes {answer_doc}, which frames the responsive position and any asserted defenses.")

    if motion_doc:
        lines.append(f"The pending application is reflected in {motion_doc}, which should be used to identify the precise relief requested and the movant's burden.")

    if opposition_doc:
        lines.append(f"The opposition record includes {opposition_doc}, which should be used to develop the factual disputes, evidentiary objections, and legal responses.")

    if order_doc:
        lines.append(f"The matter file also includes {order_doc}, which should be reviewed for any prior rulings, procedural limits, or law-of-the-case issues.")

    lines.append(
        "The attorney should replace this starter section with record-specific facts, exhibit citations, affidavit paragraph references, NYSCEF document numbers, and procedural dates."
    )

    return "\n\n".join(lines)


def build_point_headings(summary, documents):
    motion = clean_text(summary.get("motion_posture")).lower()
    selected_case = summary.get("selected_case") or {}
    rule = clean_text(selected_case.get("rule"))

    if "summary judgment" in motion:
        headings = [
            "POINT I: THE MOTION SHOULD BE DENIED BECAUSE THE MOVANT FAILED TO ESTABLISH PRIMA FACIE ENTITLEMENT TO JUDGMENT.",
            "POINT II: TRIABLE ISSUES OF FACT REQUIRE DENIAL OF SUMMARY JUDGMENT.",
            "POINT III: THE MOVING PAPERS RELY ON INCOMPLETE, INADMISSIBLE, OR DISPUTED PROOF.",
            "POINT IV: THE SELECTED AUTHORITY SUPPORTS DENIAL OR, AT MINIMUM, NARROWING OF THE REQUESTED RELIEF.",
        ]
    elif "dismiss" in motion:
        headings = [
            "POINT I: THE MOTION SHOULD BE DENIED BECAUSE THE PLEADING STATES VIABLE CLAIMS.",
            "POINT II: THE COURT MUST ACCEPT THE ALLEGATIONS AS TRUE AND DRAW FAVORABLE INFERENCES FOR THE NONMOVING PARTY.",
            "POINT III: DOCUMENTARY EVIDENCE DOES NOT CONCLUSIVELY DISPOSE OF THE CLAIMS.",
            "POINT IV: DISMISSAL IS PREMATURE OR SHOULD BE LIMITED.",
        ]
    else:
        headings = [
            "POINT I: THE MOVING PARTY HAS NOT SATISFIED ITS BURDEN.",
            "POINT II: THE RECORD PRESENTS FACTUAL AND LEGAL ISSUES REQUIRING DENIAL.",
            "POINT III: THE SELECTED AUTHORITY SUPPORTS THE OPPOSITION POSITION.",
            "POINT IV: THE REQUESTED RELIEF SHOULD BE DENIED OR NARROWED.",
        ]

    if rule:
        headings.append("POINT V: THE SELECTED RULE CONFIRMS THAT THE MOVANT'S SHOWING IS INSUFFICIENT ON THIS RECORD.")

    return headings


def build_argument_skeleton(summary, documents):
    headings = build_point_headings(summary, documents)
    skeleton = []

    for heading in headings:
        skeleton.append(
            {
                "heading": heading,
                "body": (
                    "Attorney drafting note: Insert governing standard, record facts, exhibit citations, "
                    "NYSCEF Doc. No. citations, affidavit support, and application of the selected authority to the motion record."
                ),
            }
        )

    return skeleton


def build_starter_paragraphs(summary, documents):
    motion = clean_text(summary.get("motion_posture")).lower()
    selected_case = summary.get("selected_case") or {}
    rule = clean_text(selected_case.get("rule"))
    holding = clean_text(selected_case.get("holding"))
    paragraphs = []

    if "summary judgment" in motion:
        paragraphs.extend(
            [
                "Summary judgment is a drastic remedy and should not be granted where the moving party fails to make a prima facie showing or where the record presents triable issues of fact.",
                "Here, the moving papers do not eliminate factual disputes, evidentiary issues, or competing inferences that must be resolved by the trier of fact rather than on motion practice.",
                "The opposition should first attack the movant's initial burden before addressing whether the opposing proof independently raises triable issues.",
            ]
        )
    elif "dismiss" in motion:
        paragraphs.extend(
            [
                "On a motion to dismiss, the pleading must be liberally construed, the allegations accepted as true, and the nonmoving party afforded every favorable inference.",
                "The motion should be denied where the pleading states a legally cognizable claim or where documentary evidence does not conclusively dispose of the allegations.",
                "The opposition should emphasize that factual disputes, incomplete records, and unresolved discovery issues cannot be resolved at the pleading stage.",
            ]
        )
    else:
        paragraphs.extend(
            [
                "The moving party bears the burden of establishing entitlement to the requested relief under the applicable procedural standard.",
                "The present record does not support the requested relief because key factual, legal, and evidentiary issues remain unresolved.",
                "The opposition should organize the response around burden, record defects, factual disputes, and the selected authority.",
            ]
        )

    if rule:
        paragraphs.append(f"The selected rule should be used as the principal legal anchor: {rule}")

    if holding:
        paragraphs.append(f"The selected holding should be compared directly against the facts and proof in this matter: {holding}")

    return paragraphs[:6]


def build_editable_draft_blocks(summary, documents):
    preliminary = build_preliminary_statement(summary, documents)
    facts = build_statement_of_facts(summary, documents)
    argument_skeleton = build_argument_skeleton(summary, documents)
    starter_paragraphs = build_starter_paragraphs(summary, documents)

    blocks = [
        {
            "title": "Preliminary Statement",
            "type": "textarea",
            "content": preliminary,
        },
        {
            "title": "Statement of Facts",
            "type": "textarea",
            "content": facts,
        },
    ]

    for item in argument_skeleton:
        blocks.append(
            {
                "title": item["heading"],
                "type": "textarea",
                "content": item["body"],
            }
        )

    blocks.append(
        {
            "title": "Opposition Starter Paragraphs",
            "type": "textarea",
            "content": "\n\n".join(starter_paragraphs),
        }
    )

    return blocks


def build_draft_generation(summary, documents):
    return {
        "preliminary_statement": build_preliminary_statement(summary, documents),
        "statement_of_facts": build_statement_of_facts(summary, documents),
        "point_headings": build_point_headings(summary, documents),
        "argument_skeleton": build_argument_skeleton(summary, documents),
        "starter_paragraphs": build_starter_paragraphs(summary, documents),
        "editable_blocks": build_editable_draft_blocks(summary, documents),
    }


def build_attorney_work_product(summary, documents):
    selected_case = summary.get("selected_case") or {}

    return {
        "plaintiff_core_arguments": build_plaintiff_arguments(summary, documents),
        "defense_core_arguments": build_defense_arguments(summary, documents),
        "strongest_authorities": build_authority_list(summary, documents),
        "weaknesses": build_weaknesses(summary, documents),
        "drafting_strategy": build_drafting_strategy(summary, documents),
        "recommended_outline": build_recommended_outline(summary, documents),
        "draft_generation": build_draft_generation(summary, documents),
        "citation_exhibit_engine": build_citation_exhibit_engine(summary, documents),
        "selected_rule": clean_text(selected_case.get("rule")),
        "selected_holding": clean_text(selected_case.get("holding")),
    }


def build_matter_summary(documents):
    selected = selected_case_summary(documents)
    text = combined_text(documents)

    if selected and selected.get("title"):
        case_name = selected["title"]
    else:
        case_name = extract_case_name(text)

    parties = extract_parties(case_name)

    summary = {
        "case_name": case_name,
        "index_number": extract_index_number(text),
        "plaintiff": parties["plaintiff"],
        "defendant": parties["defendant"],
        "motion_posture": detect_motion_posture(documents, text),
        "procedural_posture": detect_procedural_posture(text),
        "strongest_motion_documents": strongest_motion_documents(documents),
        "selected_case": selected,
    }

    summary["attorney_work_product"] = build_attorney_work_product(summary, documents)

    return summary


def get_matter(selected_case=None, documents=None, matter_folder=DEFAULT_MATTER_FOLDER):
    folder_documents = read_matter_folder(matter_folder)

    selected_case_document = None
    if isinstance(selected_case, dict):
        selected_case_document = selected_case_to_document(selected_case)

    if documents is None:
        documents = []

    all_documents = []

    if selected_case_document:
        all_documents.append(selected_case_document)

    all_documents.extend(folder_documents)
    all_documents.extend(documents)

    normalized_documents = [normalize_document(doc) for doc in all_documents]
    grouped_documents = group_documents(normalized_documents)
    summary = build_matter_summary(normalized_documents)

    return {
        "matter_name": summary["case_name"],
        "case_name": summary["case_name"],
        "index_number": summary["index_number"],
        "document_count": len(normalized_documents),
        "documents": normalized_documents,
        "groups": grouped_documents,
        "grouped_documents": grouped_documents,
        "folder": str(matter_folder),
        "summary": summary,
        "selected_case": summary.get("selected_case"),
        "attorney_work_product": summary.get("attorney_work_product", {}),
        "draft_generation": summary.get("attorney_work_product", {}).get("draft_generation", {}),
        "citation_exhibit_engine": summary.get("attorney_work_product", {}).get("citation_exhibit_engine", {}),
    }