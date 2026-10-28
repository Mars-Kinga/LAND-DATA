import unittest

from build_hard_shortlist import validate_shortlist


class HardShortlistTests(unittest.TestCase):
    def test_rejects_overlap_duplicate_entity_and_label_leakage(self):
        daily = [{"city": "New York", "candidate": {"name": "Example Cafe", "address": "1 Main St"}}]
        case = {"candidate_id": "two", "name": "Other Cafe", "address": "2 Main St",
                "city": "New York", "risk_signals": ["same_name_multiple_addresses"],
                "selection_signal": "same_name_branch_screening_only",
                "gold_label": None, "eligibility_checked": False}
        validate_shortlist(daily, [case])
        with self.assertRaisesRegex(ValueError, "overlaps"):
            validate_shortlist(daily, [{**case, "name": "EXAMPLE CAFE", "address": "1 MAIN ST"}])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_shortlist(daily, [case, {**case, "candidate_id": "three"}])
        with self.assertRaisesRegex(ValueError, "labels"):
            validate_shortlist(daily, [{**case, "gold_label": "verified"}])


if __name__ == "__main__":
    unittest.main()
