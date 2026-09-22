import io
import json
import unittest

from github_actions_bridge.reviewed_authority_store import (
    create_reviewed_authority_record, get_reviewed_authority_record,
    put_reviewed_authority_record,
)


CASE = "NY-Nassau-613561-2026-Desousa-v-Rennick"
SOURCE = "6394faf9d9ccdf258a061e231bf2ce9a7e27599c27e5187c4234613e876caf77"


class FakeB2:
    def __init__(self): self.objects = {}
    def get_object(self, *, Bucket, Key): return {"Body": io.BytesIO(self.objects[Key])}
    def put_object(self, *, Bucket, Key, Body, **kwargs): self.objects[Key] = Body


def record():
    return create_reviewed_authority_record(case_id=CASE, source_sha256=SOURCE, records=[{
        "authority_id": "ny-ciringione-ryan-2018-03960",
        "citation": "Ciringione v. Ryan, 162 A.D.3d 634 (2d Dep't 2018)",
        "official_primary_source": "https://www.nycourts.gov/Reporter/3dseries/2018/2018_03960.htm",
        "exact_holding": "A prescriptive easement requires hostile, open and notorious, continuous and uninterrupted use for 10 years, proved by clear and convincing evidence.",
        "filing_proposition": "The filing cites Ciringione for the prescriptive-easement elements pleaded in the complaint.",
        "filing_record_citation": "613561_2026_MICHAEL_DESOUSA_et_al_v_GEORGE_RENNICK_et_al_AFFIDAVIT_OR_AFFIRM_8 (1).pdf, p. 7",
    }])


class ReviewedAuthorityStoreTests(unittest.TestCase):
    def test_verified_record_persists_and_reads_with_all_required_review_fields(self):
        b2 = FakeB2(); stored = put_reviewed_authority_record(b2, "bucket", record())
        loaded = get_reviewed_authority_record(b2, "bucket", case_id=CASE, source_sha256=SOURCE)
        self.assertEqual(loaded, stored)
        self.assertIn("official_primary_source", loaded["records"][0])
        self.assertIn("exact_holding", loaded["records"][0])

    def test_changed_or_hash_mismatched_record_is_rejected(self):
        b2 = FakeB2(); stored = put_reviewed_authority_record(b2, "bucket", record())
        key = next(iter(b2.objects)); changed = dict(stored); changed["records"] = [dict(stored["records"][0], exact_holding="Changed")]
        b2.objects[key] = json.dumps(changed).encode()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            get_reviewed_authority_record(b2, "bucket", case_id=CASE, source_sha256=SOURCE)

    def test_incomplete_record_never_persists(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            create_reviewed_authority_record(case_id=CASE, source_sha256=SOURCE, records=[{"authority_id": "bad"}])


if __name__ == "__main__": unittest.main()
