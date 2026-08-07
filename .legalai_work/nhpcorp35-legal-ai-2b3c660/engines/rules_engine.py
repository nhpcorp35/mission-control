# rules.py
import re


def clean_text(value):
    return " ".join(str(value or "").split()).strip()


def normalize_for_search(value):
    value = str(value or "").lower()
    value = value.replace("§", " section ")
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def split_into_sentences(text):
    raw = clean_text(text)
    if not raw:
        return []

    protected = raw
    protected = re.sub(r"\bNo\.\s", "No<dot> ", protected)
    protected = re.sub(r"\bNos\.\s", "Nos<dot> ", protected)
    protected = re.sub(r"\bv\.\s", "v<dot> ", protected)
    protected = re.sub(r"\bDept\.\s", "Dept<dot> ", protected)
    protected = re.sub(r"\bInc\.\s", "Inc<dot> ", protected)
    protected = re.sub(r"\bCo\.\s", "Co<dot> ", protected)
    protected = re.sub(r"\bCorp\.\s", "Corp<dot> ", protected)
    protected = re.sub(r"\bLLC\.\s", "LLC<dot> ", protected)
    protected = re.sub(r"\bJ\.\s", "J<dot> ", protected)

    parts = re.split(r"(?<=[\.\?!])\s+(?=[A-Z])", protected)

    return [
        clean_text(p.replace("<dot>", "."))
        for p in parts
        if clean_text(p)
    ]


def is_argument_sentence(sentence):
    low = clean_text(sentence).lower()

    starters = (
        "plaintiff contends",
        "plaintiffs contend",
        "defendant contends",
        "defendants contend",
        "plaintiff argues",
        "plaintiffs argue",
        "defendant argues",
        "defendants argue",
        "plaintiff maintains",
        "plaintiffs maintain",
        "defendant maintains",
        "defendants maintain",
        "plaintiff asserts",
        "plaintiffs assert",
        "defendant asserts",
        "defendants assert",
        "plaintiff claims",
        "plaintiffs claim",
        "defendant claims",
        "defendants claim",
        "plaintiff further contends",
        "defendants further contend",
        "on appeal",
        "contrary to",
    )

    bad_phrases = (
        "contend that",
        "contends that",
        "argue that",
        "argues that",
        "maintain that",
        "maintains that",
        "assert that",
        "asserts that",
        "claim that",
        "claims that",
        "their contention",
        "his contention",
        "her contention",
        "is unavailing",
        "are unavailing",
        "without merit",
        "we reject",
        "we have considered",
        "remaining contentions",
        "need not be reached",
        "we do not reach",
        "unpreserved",
    )

    return low.startswith(starters) or any(p in low for p in bad_phrases)


def is_bad_rule_candidate(sentence):
    s = clean_text(sentence)
    low = s.lower()

    if not s or len(s) < 45:
        return True

    if is_argument_sentence(s):
        return True

    if low.startswith((
        "however,",
        "moreover,",
        "accordingly,",
        "finally,",
        "by contrast,",
        "separately,",
        "in addition,",
    )):
        return True

    bad_phrases = (
        "remaining contentions",
        "we have considered",
        "we do not reach",
        "unpreserved",
        "academic",
        "moot",
        "need not be reached",
        "without merit",
        "is unavailing",
        "are unavailing",
    )

    return any(p in low for p in bad_phrases)


def sentence_score(sentence, holding=""):
    s = clean_text(sentence)
    low = s.lower()
    holding_low = normalize_for_search(holding)
    sent_norm = normalize_for_search(s)
    score = 0

    if is_bad_rule_candidate(s):
        score -= 100

    if len(s) < 55:
        score -= 8
    elif len(s) > 420:
        score -= 6
    else:
        score += 4

    strong_phrases = [
        "the court properly",
        "supreme court correctly",
        "supreme court should have",
        "plaintiff established",
        "defendant established",
        "defendants established",
        "plaintiff failed",
        "defendant failed",
        "defendants failed",
        "failed to raise a triable issue",
        "raised a triable issue",
        "entitled to judgment as a matter of law",
        "as a matter of law",
        "triable issues of fact",
        "triable issue of fact",
        "requires dismissal",
        "survive dismissal",
        "properly dismissed",
        "properly denied",
    ]
    for phrase in strong_phrases:
        if phrase in low:
            score += 8

    reasoning_phrases = [
        "because",
        "where",
        "since",
        "given that",
        "in light of",
        "based on",
        "inasmuch as",
        "therefore",
        "thus",
        "absent evidence",
        "no evidence",
    ]
    for phrase in reasoning_phrases:
        if phrase in low:
            score += 2

    if sent_norm and holding_low and sent_norm == holding_low:
        score -= 10

    if sent_norm and holding_low and sent_norm in holding_low:
        score -= 6

    return score


def sentences_too_similar(a, b):
    a_norm = normalize_for_search(a)
    b_norm = normalize_for_search(b)
    if not a_norm or not b_norm:
        return False

    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    if not a_tokens or not b_tokens:
        return False

    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return overlap >= 0.68


def clean_sentence_for_rule(sentence):
    s = clean_text(sentence)
    if not s:
        return ""

    if is_argument_sentence(s):
        return ""

    s = re.sub(r"^Supreme Court correctly\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^Supreme Court should have\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^The court properly\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^The court correctly\s+", "", s, flags=re.IGNORECASE)

    s = re.sub(r"\s+as against\s+[A-Z][A-Za-z0-9&.,()' \-]+", "", s)
    s = re.sub(r"\([A-Z][A-Z0-9&.,' \-]{1,30}\)", "", s)
    s = re.sub(r"\([A-Z][A-Za-z0-9.,' ]+ v [A-Z][A-Za-z0-9.,' ]+\)", "", s)
    s = re.sub(r"\([^)]+AD3d[^)]+\)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;")
    s = s.replace("Labor §", "Labor Law §")

    return s


def generalize_party_phrasing(text):
    s = clean_text(text)
    if not s:
        return ""

    replacements = [
        (r"\bthe plaintiff’s\b", "a plaintiff’s"),
        (r"\bthe plaintiff's\b", "a plaintiff’s"),
        (r"\bplaintiff’s\b", "a plaintiff’s"),
        (r"\bplaintiff's\b", "a plaintiff’s"),
        (r"\bthe defendant’s\b", "a defendant’s"),
        (r"\bthe defendant's\b", "a defendant’s"),
        (r"\bdefendant’s\b", "a defendant’s"),
        (r"\bdefendant's\b", "a defendant’s"),
        (r"\bthe plaintiff\b", "a plaintiff"),
        (r"\bplaintiff\b", "a plaintiff"),
        (r"\bthe defendant\b", "a defendant"),
        (r"\bdefendant\b", "a defendant"),
        (r"\bthe defendants\b", "defendants"),
    ]
    for pattern, repl in replacements:
        s = re.sub(pattern, repl, s, flags=re.IGNORECASE)

    s = re.sub(
        r"as to whether\s+[A-Z][A-Z0-9&]{1,20}\s+actually directed or controlled\s+[A-Za-z’']+\s+injury-producing work",
        "that the defendant directed or controlled the injury-producing work",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"as to whether\s+[A-Z][A-Za-z0-9&.,'() \-]{1,60}\s+actually directed or controlled\s+[A-Za-z’']+\s+injury-producing work",
        "that the defendant directed or controlled the injury-producing work",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"as to whether\s+[A-Za-z’']+\s+actually directed or controlled\s+[A-Za-z’']+\s+injury-producing work",
        "that the defendant directed or controlled the injury-producing work",
        s,
        flags=re.IGNORECASE,
    )

    s = re.sub(r"\b[A-Z][A-Za-z0-9&]+\b(?=\s+negligence)", "a defendant", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;")
    return s


def normalize_rule_style(text):
    s = clean_text(text)
    if not s:
        return ""

    s = generalize_party_phrasing(s)
    s = s.replace("Labor §", "Labor Law §")
    s = re.sub(r"\s+", " ", s).strip(" ,.;")

    if s:
        s = s[0].upper() + s[1:]

    if not s.endswith("."):
        s += "."

    return s


def compress_rule(text, max_len=160):
    s = normalize_rule_style(text)
    if not s:
        return ""

    replacements = [
        (r"\bthe record presents\b", "the record shows"),
        (r"\braising a triable issue of fact\b", "creating a triable issue"),
        (r"\bcreating a triable issue of fact\b", "creating a triable issue"),
        (r"\btriable issues of fact\b", "triable fact issues"),
        (r"\btriable issue of fact\b", "triable issue"),
        (r"\bcommon-law\b", "common law"),
        (r"\bconstruction manager\b", "manager"),
        (r"\binjury-producing\b", "injury-causing"),
        (r"\bwhere the record presents\b", "where the record shows"),
        (r"\bwhere the record shows\b", "where"),
        (r"\bas to the defendant’s statutory responsibility\b", "as to statutory responsibility"),
        (r"\bprima facie entitlement to judgment as a matter of law\b", "entitlement to judgment as a matter of law"),
        (r"\band the opposing party fails to raise a triable issue\b", "unless the opponent raises a triable issue"),
        (r"\bshould not be dismissed where\b", "survive dismissal where"),
        (r"\bshould be dismissed absent\b", "require dismissal absent"),
    ]

    for pat, repl in replacements:
        if len(s) <= max_len:
            return s
        s = re.sub(pat, repl, s, flags=re.IGNORECASE)
        s = normalize_rule_style(s)

    if len(s) > max_len:
        trimmed = s[:max_len]
        if " " in trimmed:
            trimmed = trimmed.rsplit(" ", 1)[0]
        s = trimmed.strip(" ,.;") + "."

    return s


def expand_rule_to_min_length(text, min_len=120, max_len=160):
    s = normalize_rule_style(text)
    if not s:
        return ""

    if len(s) >= min_len:
        return compress_rule(s, max_len=max_len)

    low = s.lower()

    if "constructive trust" in low and "contract" in low:
        add = "Equitable relief is unavailable where the claim merely duplicates the contractual remedy."
    elif "triable issue" in low and "judgment as a matter of law" not in low:
        add = "The opponent must offer evidence sufficient to defeat judgment as a matter of law."
    elif "dismiss" in low and "triable issue" not in low:
        add = "Dismissal is warranted where the pleading or record fails to support the claim."
    else:
        add = "The rule turns on whether the record supplies a legally sufficient basis for relief."

    candidate = normalize_rule_style(s.rstrip(".") + ". " + add)
    return compress_rule(candidate, max_len=max_len)


def finalize_rule(text):
    s = expand_rule_to_min_length(text, min_len=120, max_len=160)
    return compress_rule(s, max_len=160)


def rule_template_contract_constructive_trust(text):
    low = clean_text(text).lower()

    has_contract = "contract" in low or "agreement" in low
    has_constructive = "constructive trust" in low
    has_duplicate = "duplicat" in low or "govern" in low or "enforceable contract" in low
    has_equity = "equitable" in low or "fraud-rectifying" in low or "intent-enforcing" in low

    if has_contract and has_constructive:
        if has_duplicate or has_equity or "breach of contract" in low:
            return finalize_rule(
                "A constructive trust claim is unavailable where an enforceable contract governs the dispute and the equitable claim merely duplicates the contract claim."
            )

    if "breach was material" in low or "excused" in low:
        return finalize_rule(
            "A material breach of a contract excuses the nonbreaching party’s further performance where the breach defeats an essential purpose of the agreement."
        )

    return ""


def rule_template_labor_200_control(sentence):
    low = sentence.lower()

    has_labor_200 = ("labor § 200" in low) or ("labor law § 200" in low)
    has_negligence = "common-law negligence" in low or "common law negligence" in low
    has_control = "directed or controlled" in low
    has_injury_work = "injury-producing work" in low or "injury-causing work" in low
    has_no_evidence = "no evidence" in low or "absent evidence" in low or "triable issue" in low

    if has_labor_200 and has_negligence and has_control and has_injury_work and has_no_evidence:
        return finalize_rule(
            "Labor Law § 200 and common-law negligence claims against a construction manager require dismissal absent a triable issue that the manager directed or controlled the injury-producing work."
        )

    if has_labor_200 and has_control and has_no_evidence:
        return finalize_rule(
            "A Labor Law § 200 claim requires dismissal absent a triable issue that the defendant directed or controlled the injury-producing work."
        )

    return ""


def rule_template_labor_240_241(sentence):
    low = sentence.lower()
    if "labor law § 240" in low and "241" in low and "declined to dismiss" in low:
        return finalize_rule(
            "Labor Law § 240(1) and § 241(6) claims survive dismissal where the record presents triable issues as to the defendant’s statutory responsibility."
        )
    return ""


def rule_template_summary_judgment(sentence):
    low = sentence.lower()

    if "entitled to judgment as a matter of law" in low and "failed to raise a triable issue" in low:
        return finalize_rule(
            "Summary judgment is warranted where the movant establishes entitlement to judgment as a matter of law and the opponent fails to raise a triable issue."
        )

    if "summary judgment" in low and "triable issue" in low and "should be denied" in low:
        return finalize_rule(
            "Summary judgment should be denied where the opposing party raises a triable issue requiring resolution by the factfinder."
        )

    if "summary judgment" in low and "triable issue" in low and "should be granted" in low:
        return finalize_rule(
            "Summary judgment should be granted where the movant establishes entitlement to judgment as a matter of law and the opponent fails to raise a triable issue."
        )

    return ""


def rule_template_claim_dismissal(sentence):
    s = clean_sentence_for_rule(sentence)
    low = s.lower()
    if not s:
        return ""

    claim_match = re.search(
        r"(?:dismissed|dismissing|dismiss)\s+(?:a plaintiff’s|the plaintiff’s|plaintiff’s|plaintiff's)\s+(.+?)\s+claims?",
        s,
        flags=re.IGNORECASE,
    )

    if claim_match and ("no evidence" in low or "triable issue" in low or "absent evidence" in low):
        claim_phrase = clean_text(claim_match.group(1)).replace("Labor §", "Labor Law §")
        if "directed or controlled" in low and "injury-producing work" in low:
            return finalize_rule(
                f"{claim_phrase} claims require dismissal absent a triable issue that the defendant directed or controlled the injury-producing work."
            )
        return finalize_rule(
            f"{claim_phrase} claims require dismissal absent a triable issue supporting the claim."
        )

    return ""


def derive_rule_from_holding(holding):
    h = clean_text(holding)
    low = h.lower()

    rule = rule_template_contract_constructive_trust(h)
    if rule:
        return rule

    if "summary judgment" in low and "breach of contract" in low and "constructive trust" in low:
        return finalize_rule(
            "A constructive trust claim is unavailable where an enforceable contract governs the dispute and the equitable claim merely duplicates the contract claim."
        )

    if "labor law § 200" in low and "common-law negligence" in low and "dismiss" in low:
        return finalize_rule(
            "Labor Law § 200 and common-law negligence claims require dismissal absent evidence that the defendant supervised, directed, or controlled the injury-producing work."
        )

    if "labor law § 240" in low and "labor law § 241" in low and "triable" in low:
        return finalize_rule(
            "Labor Law § 240(1) and § 241(6) claims survive dismissal where triable issues exist as to statutory responsibility for the work."
        )

    if "summary judgment" in low and "denied" in low:
        return finalize_rule(
            "Summary judgment should be denied where the record presents triable issues of fact requiring resolution by the factfinder."
        )

    if "summary judgment" in low and "granted" in low:
        return finalize_rule(
            "Summary judgment should be granted where the movant establishes entitlement to judgment as a matter of law and the opponent raises no triable issue."
        )

    return ""


def fallback_rule(sentence):
    s = clean_sentence_for_rule(sentence)
    if not s or is_bad_rule_candidate(s):
        return ""

    s = generalize_party_phrasing(s)
    s = re.sub(r"^(Accordingly|Here|Thus|Therefore|Moreover),?\s*", "", s, flags=re.IGNORECASE)

    s = re.sub(r"\bfailed to show\b", "fails to establish", s, flags=re.IGNORECASE)
    s = re.sub(r"\bfailed to demonstrate\b", "fails to establish", s, flags=re.IGNORECASE)
    s = re.sub(r"\bfailed to\b", "fails to", s, flags=re.IGNORECASE)
    s = re.sub(r"but for .* negligence", "causation", s, flags=re.IGNORECASE)

    if "fails to" in s.lower() and not s.lower().startswith("a claim fails"):
        s = "A claim fails where " + s.lower()

    s = re.sub(r"\s+", " ", s).strip(" ,.;")

    if s and s[0].islower():
        s = s[0].upper() + s[1:]

    if not s.endswith("."):
        s += "."

    return finalize_rule(s)


def generate_rule(best_sentence, backup_sentence="", holding=""):
    holding_rule = derive_rule_from_holding(holding)
    if holding_rule:
        return holding_rule

    candidates = [c for c in [best_sentence, backup_sentence] if c and not is_bad_rule_candidate(c)]

    for candidate in candidates:
        for fn in [
            rule_template_contract_constructive_trust,
            rule_template_labor_200_control,
            rule_template_labor_240_241,
            rule_template_summary_judgment,
            rule_template_claim_dismissal,
        ]:
            rule = fn(candidate)
            if rule:
                return rule

    for candidate in candidates:
        rule = fallback_rule(candidate)
        if rule:
            return rule

    return holding_rule or ""


def extract_holding_and_key_points(formatted_text):
    text = str(formatted_text or "").strip()
    if not text:
        return "", [], ""

    paragraphs = [clean_text(p) for p in text.split("\n\n") if clean_text(p)]
    if not paragraphs:
        return "", [], ""

    holding = paragraphs[0]
    if len(holding) > 700:
        holding = holding[:700].rsplit(" ", 1)[0] + "..."

    sentences = []
    for para in paragraphs[1:7]:
        para_sentences = split_into_sentences(para)
        for sent in para_sentences:
            if len(sent) >= 45 and not is_bad_rule_candidate(sent):
                sentences.append(sent)

    ranked = sorted(
        [(sentence_score(s, holding), s) for s in sentences],
        key=lambda x: x[0],
        reverse=True,
    )

    best_sentences = []
    for _, s in ranked:
        if any(sentences_too_similar(s, existing) for existing in best_sentences):
            continue
        best_sentences.append(s)
        if len(best_sentences) >= 2:
            break

    rule = generate_rule(
        best_sentences[0] if best_sentences else "",
        best_sentences[1] if len(best_sentences) > 1 else "",
        holding,
    )

    return holding, best_sentences, rule
