import unittest
import os
from unittest.mock import patch

from core.tasks import TargetNotFoundError, checkTargetName, ensure_all_targets_found
from utils.config import normalize_targets
import utils.config as config_module


class TaskCompletionTests(unittest.TestCase):
    def test_manual_account_filter_keeps_only_requested_account(self):
        tasks = '[{"username":"old","unique_id":"old","targets":[]},{"username":"new","unique_id":"new","targets":[]}]'
        with patch.dict(os.environ, {"TASKS": tasks, "RUN_ACCOUNT_ID": "old", "COOKIES_OLD": "[]"}, clear=False):
            config_module.userData = None
            users = config_module.get_userData()
            self.assertEqual([user["unique_id"] for user in users], ["old"])
        config_module.userData = None

    def test_missing_account_cookie_fails_instead_of_silent_success(self):
        tasks = '[{"username":"old","unique_id":"old","targets":[]}]'
        with patch.dict(os.environ, {"TASKS": tasks, "COOKIES_OLD": ""}, clear=False):
            config_module.userData = None
            with self.assertRaisesRegex(ValueError, "COOKIES_OLD"):
                config_module.get_userData()
        config_module.userData = None
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
