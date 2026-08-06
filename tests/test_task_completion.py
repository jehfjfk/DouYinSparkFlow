import unittest

from core.tasks import TargetNotFoundError, checkTargetName, ensure_all_targets_found
from utils.config import normalize_targets


class TaskCompletionTests(unittest.TestCase):
    def test_all_targets_found_succeeds(self):
        ensure_all_targets_found("account", set())

    def test_missing_target_fails_the_task(self):
        with self.assertRaisesRegex(TargetNotFoundError, "friend-a"):
            ensure_all_targets_found("account", {"friend-a"})

    def test_realtime_id_mapping_has_priority(self):
        identity_map = {
            "current-remark": ["12345", "custom-id", "sec-uid", "nickname"]
        }
        self.assertEqual(
            checkTargetName(
                "current-remark",
                ["custom-id"],
                {"custom-id": ["old-alias"]},
                identity_map,
            ),
            "custom-id",
        )

    def test_persisted_alias_is_used_when_api_mapping_is_missing(self):
        self.assertEqual(
            checkTargetName(
                "current-remark",
                ["custom-id"],
                {"custom-id": ["nickname", "current-remark"]},
                {},
            ),
            "custom-id",
        )

    def test_structured_targets_keep_ids_and_aliases(self):
        targets, aliases = normalize_targets(
            [{"id": "custom-id", "aliases": ["nickname", "remark"]}]
        )
        self.assertEqual(targets, ["custom-id"])
        self.assertEqual(aliases, {"custom-id": ["nickname", "remark"]})


if __name__ == "__main__":
    unittest.main()
