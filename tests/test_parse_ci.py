import unittest

from common import ra, check, FakeClient


class ParseRevertTests(unittest.TestCase):
    def test_git_revert_message(self):
        parsed = ra.parse_revert_message(
            'Revert "Fix cache"\n\nThis reverts commit abcdef1234567890.')
        self.assertTrue(parsed["is_revert"])
        self.assertEqual(parsed["original_sha"], "abcdef1234567890")
        self.assertEqual(parsed["subject"], "Fix cache")

    def test_short_sha(self):
        parsed = ra.parse_revert_message("This reverts commit abc1234.")
        self.assertEqual(parsed["original_sha"], "abc1234")

    def test_case_insensitive_and_extra_space(self):
        parsed = ra.parse_revert_message("This  Reverts  Commit   deadbeef")
        self.assertEqual(parsed["original_sha"], "deadbeef")

    def test_pull_reference(self):
        parsed = ra.parse_revert_message('Revert "Add API"\n\nReverts owner/repo#42')
        self.assertEqual(parsed["pr_number"], 42)
        self.assertTrue(parsed["is_revert"])

    def test_plain_pull_reference(self):
        parsed = ra.parse_revert_message('Revert "Add API"\n\nReverts #7')
        self.assertEqual(parsed["pr_number"], 7)

    def test_subject_only(self):
        parsed = ra.parse_revert_message('Revert "Something broke"')
        self.assertTrue(parsed["is_revert"])
        self.assertTrue(parsed["subject_only"])
        self.assertIsNone(parsed["original_sha"])

    def test_normal_commit_is_not_revert(self):
        parsed = ra.parse_revert_message("Fix cache; this reverts commit mental model")
        self.assertFalse(parsed["is_revert"])

    def test_revert_word_in_body_does_not_trigger_subject_only(self):
        parsed = ra.parse_revert_message('Fix docs\n\nRevert "old wording" was considered.')
        self.assertFalse(parsed["is_revert"])

    def test_empty_message(self):
        parsed = ra.parse_revert_message("")
        self.assertFalse(parsed["is_revert"])

    def test_squash_merge_suffix(self):
        parsed = ra.parse_revert_message('Revert "Add feature" (#124)')
        self.assertTrue(parsed["is_revert"])
        self.assertTrue(parsed["subject_only"])
        self.assertEqual(parsed["subject"], "Add feature")

    def test_foreign_pull_reference_is_ignored(self):
        parsed = ra.parse_revert_message(
            'Revert "X"\n\nReverts upstream/other#123', repo="local/repo")
        self.assertIsNone(parsed["pr_number"])
        self.assertEqual(parsed["pr_repo"], "upstream/other")

    def test_local_pull_reference(self):
        parsed = ra.parse_revert_message(
            'Revert "X"\n\nReverts local/repo#123', repo="local/repo")
        self.assertEqual(parsed["pr_number"], 123)

    def test_reverts_in_normal_body_is_not_enough(self):
        parsed = ra.parse_revert_message("Tidy imports\n\nFollow-up to reverts #123")
        self.assertFalse(parsed["is_revert"])

    def test_revert_of_revert_message(self):
        self.assertTrue(ra.parse_revert_message(
            'Revert "Revert "x""\n\nThis reverts commit abcdef1.')["is_revert"])


class ClassifyCiTests(unittest.TestCase):
    def test_all_success(self):
        result = ra.classify_ci([check("a"), check("b")], {})
        self.assertEqual(result.state, "green")
        self.assertEqual(result.total, 2)

    def test_failure(self):
        result = ra.classify_ci([check("a"), check("b", "failure")], {})
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.failed_checks, ["b"])

    def test_pending(self):
        result = ra.classify_ci([check("a", None, "queued")], {})
        self.assertEqual(result.state, "unknown")
        self.assertIn("pending", result.reason)

    def test_neutral_and_skipped_are_green(self):
        result = ra.classify_ci([check("a", "neutral"), check("b", "skipped")], {})
        self.assertEqual(result.state, "green")

    def test_no_checks(self):
        result = ra.classify_ci([], {})
        self.assertEqual(result.state, "unknown")
        self.assertIn("no checks", result.reason)

    def test_status_success(self):
        result = ra.classify_ci([], {"state": "success", "statuses": [
            {"context": "ci/travis", "state": "success"}]})
        self.assertEqual(result.state, "green")

    def test_status_failure(self):
        result = ra.classify_ci([], {"state": "failure", "statuses": [
            {"context": "ci/travis", "state": "failure"}]})
        self.assertEqual(result.state, "failed")

    def test_status_pending(self):
        result = ra.classify_ci([], {"state": "pending", "statuses": [
            {"context": "ci/travis", "state": "pending"}]})
        self.assertEqual(result.state, "unknown")

    def test_checks_plus_failed_status(self):
        result = ra.classify_ci([check("a")], {"statuses": [
            {"context": "legacy", "state": "error"}]})
        self.assertEqual(result.state, "failed")
        self.assertIn("legacy", result.failed_checks)

    def test_unknown_conclusion_is_pending(self):
        result = ra.classify_ci([check("a", "mystery")], {})
        self.assertEqual(result.state, "unknown")

    def test_status_state_failure_wins_over_success_list(self):
        result = ra.classify_ci([], {"state": "failure", "statuses": [
            {"context": "ci/travis", "state": "success"}]})
        self.assertEqual(result.state, "failed")
        self.assertIn("combined-status", result.failed_checks)

    def test_duplicate_contexts_are_not_double_counted(self):
        result = ra.classify_ci([check("a")], {"statuses": [
            {"context": "a", "state": "success"}]})
        self.assertEqual(result.total, 1)
        self.assertEqual(result.state, "green")


class EvaluateCiTests(unittest.TestCase):
    def test_head_green(self):
        client = FakeClient(checks={"head": [check("a")]})
        result = ra.evaluate_ci(client, ["head", "merge"])
        self.assertEqual(result.state, "green")
        self.assertEqual(result.source, "head")

    def test_falls_back_to_merge(self):
        client = FakeClient(checks={"head": [], "merge": [check("a")]})
        result = ra.evaluate_ci(client, ["head", "merge"])
        self.assertEqual(result.state, "green")
        self.assertEqual(result.source, "merge")

    def test_failed_head_is_definitive(self):
        client = FakeClient(checks={"head": [check("a", "failure")],
                                    "merge": [check("a")]})
        result = ra.evaluate_ci(client, ["head", "merge"])
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.source, "head")

    def test_both_unknown(self):
        client = FakeClient()
        result = ra.evaluate_ci(client, ["head", "merge"])
        self.assertEqual(result.state, "unknown")

    def test_offline_miss_falls_through(self):
        client = FakeClient(misses={"head"}, checks={"merge": [check("a")]})
        result = ra.evaluate_ci(client, ["head", "merge"])
        self.assertEqual(result.state, "green")

    def test_api_error_falls_through(self):
        client = FakeClient(errors={"head": ra.ApiError(403, "actions disabled")},
                            checks={"merge": [check("a")]})
        result = ra.evaluate_ci(client, ["head", "merge"])
        self.assertEqual(result.state, "green")

    def test_rate_limit_propagates(self):
        client = FakeClient(errors={"head": ra.RateLimitError(None, "limit")})
        with self.assertRaises(ra.RateLimitError):
            ra.evaluate_ci(client, ["head"])

    def test_duplicate_candidates_are_evaluated_once(self):
        client = FakeClient(checks={"head": [check("a")]})
        result = ra.evaluate_ci(client, ["head", "head"])
        self.assertEqual(result.state, "green")


if __name__ == "__main__":
    unittest.main()
