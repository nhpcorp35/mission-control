"""Tests for Case-00 attorney-feedback evaluation loop."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from case00_attorney_eval.adapter import (
    Case00QuestionBundle,
    ProvisionalAnswerArtifact,
)
from case00_attorney_eval.evaluate import (
    ANSWER_VERSION_CANDIDATE,
    ANSWER_VERSION_ORIGINAL,
    REF_STATUS_ATTORNEY_APPROVED,
    REF_STATUS_NONE,
    REF_STATUS_PROVISIONAL,
    REF_STATUS_PROVISIONAL_PLACEHOLDER,
    evaluate_case00,
    evaluate_question,
    format_human_summary,
)
from case00_attorney_eval.scoring import (
    SCORING_DIMENSIONS,
    STATUS_NOT_EVALUABLE,
    STATUS_PENDING_ATTORNEY_SCORE,
    score_dimensions,
)


def _label(
    qid: str,
    *,
    status: str = "final",
    locked: bool = True,
    verdict: str = "incorrect",
    materiality: str = "material",
    categories: list | None = None,
    scores: dict | None = None,
) -> dict:
    return {
        "question_id": qid,
        "question_text": f"Text for {qid}",
        "label_status": status,
        "locked": locked,
        "attorney_verdict": verdict,
        "materiality": materiality,
        "primary_error_categories": categories if categories is not None else ["x"],
        "attorney_notes_summary": f"Notes for {qid}",
        "rubric_dimension_scores": scores
        if scores is not None
        else {d: None for d in SCORING_DIMENSIONS},
        "corrective_evidence_target": {"nyscef_document": 1},
        "safety_gate": {"material_error_gate": True, "provisional": status != "final"},
    }


def _bundle(
    qid: str = "Q1",
    *,
    original: str | None = "original answer",
    label: dict | None = None,
    provisional: ProvisionalAnswerArtifact | None = None,
    attorney_approved=None,
) -> Case00QuestionBundle:
    return Case00QuestionBundle(
        question_id=qid,
        question_text=f"Question {qid}?",
        original_legalai_answer=original,
        label_record=label if label is not None else _label(qid),
        provisional=provisional,
        attorney_approved=attorney_approved,
    )


class ProvisionalVersusFinalStatusTests(unittest.TestCase):
    def test_provisional_is_never_attorney_approved(self):
        prov = ProvisionalAnswerArtifact(
            question_id="Q1",
            path=Path("Q1_provisional_gold_answer.md"),
            status="provisional_awaiting_john_approval",
            is_placeholder=False,
            body="Corrected provisional text",
            raw_markdown="x",
        )
        record = evaluate_question(
            _bundle(provisional=prov),
            review_status={"packet_approved_by_attorney": False},
        )
        ref = record["reference_answer_status"]
        self.assertEqual(ref["status"], REF_STATUS_PROVISIONAL)
        self.assertFalse(ref["provisional_treated_as_approved"])
        self.assertTrue(ref["usable_reference"])
        self.assertEqual(ref["reference_text_source"], "provisional_corrected")
        self.assertFalse(record["flags"]["has_final_attorney_approved_answer"])
        self.assertFalse(record["flags"]["provisional_silently_promoted"])

    def test_attorney_approved_status_requires_explicit_artifact(self):
        from case00_attorney_eval.adapter import AttorneyApprovedAnswerArtifact

        approved = AttorneyApprovedAnswerArtifact(
            question_id="Q2",
            path=Path("Q2_attorney_approved_gold_answer.md"),
            body="Approved gold",
            approval_marker="attorney_approved_gold",
        )
        prov = ProvisionalAnswerArtifact(
            question_id="Q2",
            path=Path("Q2_provisional_gold_answer.md"),
            status="provisionally_accepted_substance_awaiting_formal_gold_approval",
            is_placeholder=False,
            body="Still provisional",
            raw_markdown="x",
        )
        record = evaluate_question(
            _bundle(
                "Q2",
                label=_label("Q2", verdict="correct", materiality="none", categories=[]),
                provisional=prov,
                attorney_approved=approved,
            ),
            review_status={
                "packet_approved_by_attorney": True,
                "incomplete_overall_comment": False,
            },
        )
        self.assertEqual(
            record["reference_answer_status"]["status"], REF_STATUS_ATTORNEY_APPROVED
        )
        self.assertFalse(
            record["reference_answer_status"]["provisional_treated_as_approved"]
        )
        self.assertEqual(
            record["reference_answer_status"]["reference_text_source"],
            "attorney_approved",
        )


class MissingReferenceTests(unittest.TestCase):
    def test_missing_reference_status_and_not_evaluable_scores(self):
        record = evaluate_question(
            _bundle(provisional=None, attorney_approved=None),
            review_status={},
        )
        self.assertEqual(record["reference_answer_status"]["status"], REF_STATUS_NONE)
        self.assertFalse(record["reference_answer_status"]["usable_reference"])
        self.assertIn(
            "no provisional or attorney-approved reference answer",
            record["missing_information_preventing_complete_scoring"],
        )
        for dim in SCORING_DIMENSIONS:
            self.assertEqual(record["scoring"][dim]["status"], STATUS_NOT_EVALUABLE)
            self.assertIsNone(record["scoring"][dim]["score"])
        self.assertEqual(record["scoring_dimensions_currently_evaluable"], [])

    def test_placeholder_provisional_is_not_usable_reference(self):
        prov = ProvisionalAnswerArtifact(
            question_id="Q5",
            path=Path("Q5_provisional_gold_answer.md"),
            status="placeholder_pending_reviewer_completion",
            is_placeholder=True,
            body=None,
            raw_markdown="DRAFTING BLOCKED",
        )
        record = evaluate_question(
            _bundle("Q5", provisional=prov),
            review_status={"incomplete_overall_comment": True},
        )
        self.assertEqual(
            record["reference_answer_status"]["status"],
            REF_STATUS_PROVISIONAL_PLACEHOLDER,
        )
        self.assertFalse(record["flags"]["has_provisional_answer"])
        self.assertTrue(record["flags"]["has_provisional_placeholder"])
        self.assertFalse(record["flags"]["ready_for_automated_evaluation"])


class OriginalPreservationTests(unittest.TestCase):
    def test_candidate_evaluation_preserves_original(self):
        original = "ORIGINAL LEGALAI ANSWER — DO NOT LOSE"
        record = evaluate_question(
            _bundle(original=original),
            review_status={},
            answer_version=ANSWER_VERSION_CANDIDATE,
            candidate_answer="newer model answer",
            preserve_original=True,
        )
        self.assertEqual(record["answer_version_evaluated"], ANSWER_VERSION_CANDIDATE)
        self.assertEqual(record["evaluated_answer"], "newer model answer")
        self.assertEqual(record["preserved_original_legalai_answer"], original)
        self.assertTrue(record["original_preserved"])
        # Original must remain distinct from candidate.
        self.assertNotEqual(
            record["evaluated_answer"], record["preserved_original_legalai_answer"]
        )


class NoFabricatedScoresTests(unittest.TestCase):
    def test_null_label_scores_remain_null(self):
        scored = score_dimensions(
            _label("Q1"),
            has_reference_answer=True,
            reference_status=REF_STATUS_PROVISIONAL,
        )
        self.assertEqual(scored["currently_evaluable"], [])
        for dim in SCORING_DIMENSIONS:
            self.assertIsNone(scored["dimensions"][dim]["score"])
            self.assertEqual(
                scored["dimensions"][dim]["status"], STATUS_PENDING_ATTORNEY_SCORE
            )

    def test_evaluation_record_never_invents_numeric_scores(self):
        prov = ProvisionalAnswerArtifact(
            question_id="Q1",
            path=Path("Q1.md"),
            status="provisional_awaiting_john_approval",
            is_placeholder=False,
            body="prov",
            raw_markdown="x",
        )
        record = evaluate_question(
            _bundle(provisional=prov),
            review_status={},
        )
        self.assertFalse(record["flags"]["fabricated_scores"])
        for dim, payload in record["scoring"].items():
            self.assertIsNone(payload["score"], msg=dim)
            self.assertIn(
                payload["status"],
                {STATUS_PENDING_ATTORNEY_SCORE, STATUS_NOT_EVALUABLE},
            )

    def test_explicit_attorney_score_is_passed_through(self):
        scores = {d: None for d in SCORING_DIMENSIONS}
        scores["factual_accuracy"] = 2
        scored = score_dimensions(
            _label("Q1", scores=scores),
            has_reference_answer=True,
            reference_status=REF_STATUS_ATTORNEY_APPROVED,
        )
        self.assertEqual(scored["dimensions"]["factual_accuracy"]["score"], 2)
        self.assertEqual(scored["dimensions"]["factual_accuracy"]["status"], "scored")
        self.assertIn("factual_accuracy", scored["currently_evaluable"])


class SummaryCountTests(unittest.TestCase):
    def _write_mini_corpus(self, root: Path) -> None:
        gold = root / "derived" / "attorney-gold-benchmark-01"
        packet_dir = root / "derived" / "attorney-review-packet-02-live"
        prov = gold / "provisional-gold-answers"
        gold.mkdir(parents=True)
        packet_dir.mkdir(parents=True)
        prov.mkdir(parents=True)

        labels = {
            "schema_version": "attorney_gold_labels_01.v1",
            "corpus_id": "case-00-triborough",
            "benchmark_id": "attorney-gold-benchmark-01",
            "packet_id": "attorney-review-packet-02-live",
            "review_status": {
                "packet_approved_by_attorney": False,
                "legalai_approved_by_attorney": False,
                "incomplete_overall_comment": True,
                "q1_q3": "final_locked",
                "q4_q5": "provisional_unlocked",
            },
            "rubric": {
                "name": "five_dimension_attorney_gold_rubric",
                "scale": {"min": 0, "max": 4},
                "scoring_note": "Do not fabricate scores",
                "packet_mean": None,
                "packet_mean_status": "not_calculated",
                "dimensions": [{"id": d} for d in SCORING_DIMENSIONS],
            },
            "question_records": [
                _label("Q1", status="final", locked=True, verdict="incorrect"),
                _label(
                    "Q2",
                    status="final",
                    locked=True,
                    verdict="correct",
                    materiality="none",
                    categories=[],
                ),
                _label(
                    "Q3",
                    status="provisional",
                    locked=False,
                    verdict="partially_correct",
                ),
            ],
        }
        (gold / "attorney_gold_labels_01.json").write_text(
            json.dumps(labels), encoding="utf-8"
        )

        packet = {
            "artifact_type": "attorney_review_packet_02",
            "questions": [
                {
                    "question_id": "Q1",
                    "text": "Q1 text",
                    "proposed_answer": "original one",
                    "answer_status": "live_openai_validated",
                    "reasoner_status": "READY",
                    "confidence": 0.5,
                },
                {
                    "question_id": "Q2",
                    "text": "Q2 text",
                    "proposed_answer": "original two",
                    "answer_status": "live_openai_validated",
                    "reasoner_status": "READY",
                    "confidence": 0.5,
                },
                {
                    "question_id": "Q3",
                    "text": "Q3 text",
                    "proposed_answer": "original three",
                    "answer_status": "live_openai_validated",
                    "reasoner_status": "READY",
                    "confidence": 0.5,
                },
                {
                    "question_id": "Q4",
                    "text": "Q4 no labels",
                    "proposed_answer": "original four",
                    "answer_status": "live_openai_validated",
                    "reasoner_status": "READY",
                    "confidence": 0.5,
                },
            ],
        }
        (packet_dir / "attorney_review_packet_02.json").write_text(
            json.dumps(packet), encoding="utf-8"
        )

        (prov / "Q1_provisional_gold_answer.md").write_text(
            "# Q1\n\n## 2. Status\n\n`provisional_awaiting_john_approval`\n\n"
            "## 3. Provisional gold answer\n\nCorrected Q1 body.\n\n"
            "## 4. Required\n\nKeep.\n",
            encoding="utf-8",
        )
        (prov / "Q2_provisional_gold_answer.md").write_text(
            "# Q2\n\n## 2. Status\n\n"
            "`provisionally_accepted_substance_awaiting_formal_gold_approval`\n\n"
            "## 3. Provisional gold answer\n\nCorrected Q2 body.\n\n"
            "## 4. Required\n\nKeep.\n",
            encoding="utf-8",
        )
        (prov / "Q3_provisional_gold_answer.md").write_text(
            "# Q3\n\n## 2. Status\n\n`placeholder_pending_reviewer_completion`\n\n"
            "## 3. Provisional gold answer\n\n"
            "**DRAFTING BLOCKED — NO SUBSTANTIVE REPLACEMENT ANSWER.**\n\n"
            "## 4. Required\n\nPending.\n",
            encoding="utf-8",
        )

    def test_summary_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_corpus(root)
            result = evaluate_case00(root)
            summary = result["summary"]
            self.assertEqual(summary["total_questions"], 4)
            self.assertEqual(summary["questions_with_original_answers"], 4)
            self.assertEqual(summary["questions_with_provisional_answers"], 2)
            self.assertEqual(
                summary["questions_with_final_attorney_approved_answers"], 0
            )
            self.assertEqual(summary["questions_with_complete_diagnostic_labels"], 3)
            # Q1 and Q2 have usable provisional + complete labels.
            self.assertEqual(summary["questions_ready_for_automated_evaluation"], 2)
            self.assertEqual(
                set(summary["question_id_lists"]["ready_for_automated_evaluation"]),
                {"Q1", "Q2"},
            )
            # No attorney-approved answers + incomplete packet => all need review.
            self.assertEqual(
                summary["questions_still_requiring_attorney_review"], 4
            )
            human = format_human_summary(result)
            self.assertIn("Questions with provisional answers: 2", human)
            self.assertIn(
                "Questions with final attorney-approved answers: 0", human
            )

    def test_candidate_rerun_preserves_all_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_mini_corpus(root)
            result = evaluate_case00(
                root,
                candidate_answers={"Q1": "brand new answer"},
            )
            q1 = next(q for q in result["questions"] if q["question_id"] == "Q1")
            self.assertEqual(q1["answer_version_evaluated"], ANSWER_VERSION_CANDIDATE)
            self.assertEqual(q1["evaluated_answer"], "brand new answer")
            self.assertEqual(
                q1["preserved_original_legalai_answer"], "original one"
            )
            q2 = next(q for q in result["questions"] if q["question_id"] == "Q2")
            self.assertEqual(q2["answer_version_evaluated"], ANSWER_VERSION_ORIGINAL)
            self.assertEqual(q2["preserved_original_legalai_answer"], "original two")


@unittest.skipUnless(
    Path("/app/data/case-00-triborough/derived/attorney-gold-benchmark-01").is_dir(),
    "Live Case-00 gold benchmark volume unavailable",
)
class LiveCase00IntegrationTests(unittest.TestCase):
    def test_live_corpus_loads_and_does_not_fabricate(self):
        result = evaluate_case00("/app/data/case-00-triborough")
        summary = result["summary"]
        self.assertEqual(summary["total_questions"], 5)
        self.assertEqual(summary["questions_with_original_answers"], 5)
        self.assertEqual(summary["questions_with_provisional_answers"], 4)
        self.assertEqual(
            summary["questions_with_final_attorney_approved_answers"], 0
        )
        self.assertEqual(summary["questions_with_complete_diagnostic_labels"], 5)
        self.assertEqual(summary["questions_ready_for_automated_evaluation"], 4)
        self.assertEqual(summary["questions_still_requiring_attorney_review"], 5)
        for q in result["questions"]:
            self.assertFalse(q["flags"]["fabricated_scores"])
            self.assertFalse(q["flags"]["provisional_silently_promoted"])
            self.assertFalse(
                q["reference_answer_status"]["provisional_treated_as_approved"]
            )
            for dim, payload in q["scoring"].items():
                self.assertIsNone(payload["score"], msg=f"{q['question_id']} {dim}")
        q5 = next(q for q in result["questions"] if q["question_id"] == "Q5")
        self.assertEqual(
            q5["reference_answer_status"]["status"],
            REF_STATUS_PROVISIONAL_PLACEHOLDER,
        )


if __name__ == "__main__":
    unittest.main()
