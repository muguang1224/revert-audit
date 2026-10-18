import unittest

from common import commit, pr, check, FakeClient, ra


def basic(revert_sha="rev0001", original_sha="a1b2c3d", date="2026-02-01T00:00:00Z",
          revert_date="2026-02-05T00:00:00Z", files=None, checks=None,
          message=None):
    message = message or ('Revert "Change"\n\nThis reverts commit %s.' % original_sha)
    commits = [commit(revert_sha, message, date=revert_date)]
    details = {original_sha: commit(original_sha, "Change\n\nBody", date=date,
                                    files=(files if files is not None
                                           else [{"filename": "src/a.py"}]),
                                    stats={"additions": 10, "deletions": 2})}
    return FakeClient(commits=commits, details=details,
                      checks=checks if checks is not None else {original_sha: [check("test")]})


class AnalyzeTests(unittest.TestCase):
    def test_green_revert(self):
        report = ra.analyze("o/r", basic())
        self.assertEqual(len(report.records), 1)
        record = report.records[0]
        self.assertEqual(record.ci, "green")
        self.assertEqual(record.original_sha, "a1b2c3d")
        self.assertEqual(record.time_to_revert_days, 4.0)
        summary = ra.summarize(report)
        self.assertEqual(summary["green_rate"], 1.0)
        self.assertEqual(summary["median_change_lines"], 12.0)

    def test_failed_revert(self):
        client = basic(checks={"a1b2c3d": [check("test", "failure")]})
        summary = ra.summarize(ra.analyze("o/r", client))
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["green_rate"], 0.0)

    def test_unknown_ci(self):
        client = basic(checks={})
        summary = ra.summarize(ra.analyze("o/r", client))
        self.assertEqual(summary["unknown"], 1)
        self.assertIsNone(summary["green_rate"])

    def test_subject_only_is_unidentified(self):
        client = FakeClient(commits=[commit("rev1", 'Revert "Gone"')])
        report = ra.analyze("o/r", client)
        self.assertEqual(len(report.records), 1)
        self.assertTrue(report.records[0].unidentified)
        self.assertEqual(ra.summarize(report)["unknown"], 0)

    def test_original_not_found(self):
        client = FakeClient(commits=[commit("rev1", "This reverts commit abcdef1.")])
        report = ra.analyze("o/r", client)
        self.assertTrue(report.records[0].unidentified)

    def test_pull_revert_uses_pr_head(self):
        client = FakeClient(
            commits=[commit("rev1", 'Revert "Change"\n\nReverts #7')],
            pulls={7: pr(7, "head1")},
            details={"head1": commit("head1", "Change", files=[{"filename": "a.py"}])},
            checks={"head1": [check("ci")]},
        )
        report = ra.analyze("o/r", client)
        record = report.records[0]
        self.assertEqual(record.ci, "green")
        self.assertEqual(record.ci_source, "head1")
        self.assertEqual(record.pr_number, 7)

    def test_pull_found_from_commit(self):
        client = basic()
        client.pulls_for = {"a1b2c3d": [pr(9, "a1b2c3d")]}
        record = ra.analyze("o/r", client).records[0]
        self.assertEqual(record.pr_number, 9)

    def test_revert_of_revert_is_skipped(self):
        revert_message = "This reverts commit a1b2c3d."
        client = FakeClient(
            commits=[commit("rev2", "Revert \"Revert\"\n\nThis reverts commit b1c2d3e.")],
            details={"b1c2d3e": commit("b1c2d3e", revert_message)},
            checks={},
        )
        report = ra.analyze("o/r", client)
        self.assertTrue(report.records[0].relapse)
        self.assertEqual(report.records[0].ci, "skipped")
        summary = ra.summarize(report)
        self.assertEqual(summary["known"], 0)
        self.assertIsNone(summary["green_rate"])

    def test_duplicate_original_is_counted_once(self):
        message = "This reverts commit a1b2c3d."
        client = FakeClient(
            commits=[commit("rev1", message), commit("rev2", message)],
            details={"a1b2c3d": commit("a1b2c3d", "Change")},
            checks={"a1b2c3d": [check("ci")]},
        )
        report = ra.analyze("o/r", client)
        self.assertEqual(len(report.records), 1)
        self.assertEqual(report.duplicates, 1)

    def test_api_error_degrades_to_unidentified(self):
        client = FakeClient(
            commits=[commit("rev1", "This reverts commit a1b2c3d.")],
            commit_errors={"a1b2c3d": ra.ApiError(500, "boom")},
        )
        report = ra.analyze("o/r", client)
        self.assertTrue(report.records[0].unidentified)
        self.assertIn("API error", report.records[0].reason)
        self.assertTrue(report.warnings)

    def test_unidentified_keeps_parsed_sha_and_kind(self):
        client = FakeClient(commits=[commit("rev1", "This reverts commit abcdef1.")])
        record = ra.analyze("o/r", client).records[0]
        self.assertEqual(record.original_sha, "abcdef1")
        self.assertEqual(record.kind, "commit")

    def test_merge_commit_uses_pull_stats(self):
        pull = pr(7, "head1")
        pull.update({"changed_files": 42, "additions": 900, "deletions": 100})
        client = FakeClient(
            commits=[commit("rev1", 'Revert "Big"\n\nReverts #7')],
            pulls={7: pull},
            details={"head1": commit("head1", "Big", files=[])},
        )
        record = ra.analyze("o/r", client).records[0]
        self.assertEqual(record.files, 42)
        self.assertEqual(record.additions, 900)
        self.assertEqual(record.deletions, 100)

    def test_pull_for_commit_chooses_earliest(self):
        early = pr(40, "a1b2c3d", merged_at="2026-01-02T00:00:00Z", title="Feature")
        late = pr(500, "a1b2c3d", merged_at="2026-06-01T00:00:00Z", title="Release")
        client = basic()
        client.pulls_for = {"a1b2c3d": [late, early]}
        record = ra.analyze("o/r", client).records[0]
        self.assertEqual(record.pr_number, 40)
        self.assertGreaterEqual(record.time_to_revert_days, 0)

    def test_revert_aware_title_is_not_a_relapse(self):
        pull = pr(9, "a1b2c3d", title="Revert-aware merge queue")
        client = basic()
        client.pulls_for = {"a1b2c3d": [pull]}
        record = ra.analyze("o/r", client).records[0]
        self.assertFalse(record.relapse)
        self.assertEqual(record.ci, "green")

    def test_common_test_layouts(self):
        for name in ("app/tests.py", "tests/test_x.py", "FooTest.php",
                     "foo_spec.rb", "src/test/java/FooTest.java", "testdata/x.json"):
            self.assertTrue(ra._tests_touched([{"filename": name}]), name)
        for name in ("src/latest.py", "src/contest.py", "src/attestation.md"):
            self.assertFalse(ra._tests_touched([{"filename": name}]), name)

    def test_scanned_counts_examined_prefix(self):
        commits = [commit("d%06x" % i, "This reverts commit a%06x." % i) for i in range(5)]
        details = {"a%06x" % i: commit("a%06x" % i, "Change") for i in range(5)}
        client = FakeClient(commits=commits, details=details)
        report = ra.analyze("o/r", client, max_reverts=2)
        self.assertEqual(report.scanned, 3)
        self.assertEqual(report.revert_commits, 2)
        self.assertTrue(report.truncated)

    def test_tests_touched(self):
        client = basic(files=[{"filename": "tests/test_a.py"}, {"filename": "src/a.py"}])
        record = ra.analyze("o/r", client).records[0]
        self.assertTrue(record.tests_touched)

    def test_tests_not_touched(self):
        client = basic(files=[{"filename": "src/a.py"}])
        self.assertFalse(ra.analyze("o/r", client).records[0].tests_touched)

    def test_missing_files(self):
        client = basic(files=[])
        summary = ra.summarize(ra.analyze("o/r", client))
        self.assertEqual(summary["files_known"], 0)
        self.assertIsNone(summary["tests_touched_rate"])

    def test_max_reverts_truncates(self):
        shas = ["a%06x" % i for i in range(5)]
        commits = [commit("d%06x" % i, "This reverts commit %s." % shas[i]) for i in range(5)]
        details = {shas[i]: commit(shas[i], "Change") for i in range(5)}
        client = FakeClient(commits=commits, details=details)
        report = ra.analyze("o/r", client, max_reverts=2)
        self.assertEqual(len(report.records), 2)
        self.assertTrue(report.truncated)

    def test_since_is_passed(self):
        client = basic()
        ra.analyze("o/r", client, since="2025-01-01T00:00:00Z")
        self.assertEqual(client.since, "2025-01-01T00:00:00Z")

    def test_commits_without_files_have_zero(self):
        client = basic(files=[])
        record = ra.analyze("o/r", client).records[0]
        self.assertEqual(record.files, 0)


class SummaryTests(unittest.TestCase):
    def test_green_rate(self):
        records = []
        for i, ci in enumerate(["green", "green", "failed", "unknown"]):
            records.append(ra.Record(
                "r%d" % i, "2026-01-01", "s", "o%d" % i, "s", "2026-01-01",
                None, "commit", False, False, ci, "", [], [], 1, 1.0,
                1, 1, 1, False, ""))
        report = ra.Report("o/r", 10, None, 10, False, records)
        summary = ra.summarize(report)
        self.assertEqual(summary["known"], 3)
        self.assertAlmostEqual(summary["green_rate"], 0.6667)
        self.assertEqual(summary["green"], 2)

    def test_no_known(self):
        report = ra.Report("o/r", 1, None, 1, False, [])
        summary = ra.summarize(report)
        self.assertIsNone(summary["green_rate"])
        self.assertEqual(summary["median_time_to_revert_days"], None)

    def test_top_checks(self):
        records = [ra.Record(
            "r", "2026-01-01", "s", "o", "s", "2026-01-01", None, "commit",
            False, False, "green", "", ["test", "lint"], [], 2, 1.0,
            1, 1, 1, False, "")]
        summary = ra.summarize(ra.Report("o/r", 1, None, 1, False, records))
        self.assertEqual(summary["top_green_checks"], [("lint", 1), ("test", 1)])


if __name__ == "__main__":
    unittest.main()
