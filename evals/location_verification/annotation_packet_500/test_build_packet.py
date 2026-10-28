import unittest

from build_packet import select


def rows():
    return [
        {"screening_id": f"case-{i:04d}", "city": "New York" if i < 800 else "Hanoi",
         "candidate": {"entity_group_digest": f"entity-{i:04d}"}}
        for i in range(1000)
    ]


class PacketTests(unittest.TestCase):
    def test_selection_has_500_unique_entities_and_is_stable(self):
        source = rows()
        first = select(source)
        self.assertEqual(len(first), 500)
        self.assertEqual(sum(row["city"] == "New York" for row in first), 400)
        self.assertEqual(sum(row["city"] == "Hanoi" for row in first), 100)
        self.assertEqual(len({row["candidate"]["entity_group_digest"] for row in first}), 500)
        self.assertEqual(first, select(list(reversed(source))))

    def test_old_status_or_duplicate_entity_fails_closed(self):
        contaminated = rows()
        contaminated[3]["candidate"]["review_status"] = "AUTO_APPROVED"
        with self.assertRaisesRegex(ValueError, "contamination"):
            select(contaminated)
        duplicate = rows()
        duplicate[3]["candidate"]["entity_group_digest"] = duplicate[4]["candidate"]["entity_group_digest"]
        with self.assertRaisesRegex(ValueError, "not unique"):
            select(duplicate)


if __name__ == "__main__":
    unittest.main()
