import unittest

from core.tasks import TargetNotFoundError, ensure_all_targets_found


class TaskCompletionTests(unittest.TestCase):
    def test_all_targets_found_succeeds(self):
        ensure_all_targets_found("account", set())

    def test_missing_target_fails_the_task(self):
        with self.assertRaisesRegex(TargetNotFoundError, "friend-a"):
            ensure_all_targets_found("account", {"friend-a"})


if __name__ == "__main__":
    unittest.main()
