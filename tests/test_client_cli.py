import json
import unittest
import urllib.parse

from common import commit, check, FakeClient, ra, tmpdir, run_main


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, url, headers):
        self.calls.append((url, headers))
        return self.handler(url, headers)


class ClientTests(unittest.TestCase):
    def test_cache_round_trip_and_etag(self):
        cache = tmpdir(self)
        calls = []

        def first(url, headers):
            calls.append(headers.get("If-None-Match"))
            return 200, {"ETag": "e1", "X-RateLimit-Remaining": "9"}, b'{"a": 1}'

        client = ra.GitHubClient("o/r", token="t", api_base="https://example.test",
                                 cache_dir=cache, transport=FakeTransport(first))
        self.assertEqual(client.get("/x"), {"a": 1})
        self.assertEqual(client.remaining, 9)

        def second(url, headers):
            calls.append(headers.get("If-None-Match"))
            return 304, {}, b""

        client2 = ra.GitHubClient("o/r", token="t", api_base="https://example.test",
                                  cache_dir=cache, transport=FakeTransport(second))
        self.assertEqual(client2.get("/x"), {"a": 1})
        self.assertEqual(calls, [None, "e1"])

    def test_offline_reads_cache(self):
        cache = tmpdir(self)
        transport = FakeTransport(lambda u, h: (200, {"ETag": "e"}, b'{"a": 2}'))
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 cache_dir=cache, transport=transport)
        client.get("/x")
        offline = ra.GitHubClient("o/r", api_base="https://example.test",
                                  cache_dir=cache, offline=True,
                                  transport=FakeTransport(lambda u, h: (500, {}, b"")))
        self.assertEqual(offline.get("/x"), {"a": 2})

    def test_offline_miss(self):
        cache = tmpdir(self)
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 cache_dir=cache, offline=True)
        with self.assertRaises(ra.OfflineMiss):
            client.get("/missing")

    def test_404_allow(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (404, {}, b'{"message":"nope"}')))
        self.assertIsNone(client.get("/x", allow_404=True))

    def test_404_raises(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (404, {}, b'{"message":"nope"}')))
        with self.assertRaises(ra.NotFound):
            client.get("/x")

    def test_401_is_auth_error(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (401, {}, b'{"message":"bad"}')) )
        with self.assertRaises(ra.AuthError):
            client.get("/x")

    def test_rate_limit(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (
                                     403, {"X-RateLimit-Remaining": "0",
                                           "X-RateLimit-Reset": "1700000000"},
                                     b'{"message":"API rate limit exceeded"}')))
        with self.assertRaises(ra.RateLimitError) as ctx:
            client.get("/x")
        self.assertEqual(ctx.exception.reset, 1700000000)

    def test_forbidden_without_rate_limit(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (
                                     403, {}, b'{"message":"Resource not accessible"}')))
        with self.assertRaises(ra.ApiError) as ctx:
            client.get("/x")
        self.assertEqual(ctx.exception.status, 403)

    def test_invalid_json(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (200, {}, b"not json")))
        with self.assertRaises(ra.ApiError):
            client.get("/x")

    def test_corrupt_cache_is_a_miss(self):
        cache = tmpdir(self)
        key = ra.Cache.key("https://example.test/x")
        (cache / (key + ".json")).write_text("123", encoding="utf-8")
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 cache_dir=cache, offline=True)
        with self.assertRaises(ra.OfflineMiss):
            client.get("/x")

    def test_cache_entry_without_body_is_a_miss(self):
        cache = tmpdir(self)
        key = ra.Cache.key("https://example.test/x")
        (cache / (key + ".json")).write_text('{"etag": "x"}', encoding="utf-8")
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 cache_dir=cache, offline=True)
        with self.assertRaises(ra.OfflineMiss):
            client.get("/x")

    def test_cache_dir_that_is_a_file(self):
        base = tmpdir(self)
        path = base / "cache"
        path.write_text("x", encoding="utf-8")
        with self.assertRaises(ra.ToolError):
            ra.GitHubClient("o/r", api_base="https://example.test", cache_dir=path)

    def test_empty_200_body_is_an_error(self):
        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(lambda u, h: (200, {}, b"")))
        with self.assertRaises(ra.ApiError):
            client.get("/x")

    def test_duplicate_pages_stop(self):
        def handler(url, headers):
            items = [{"sha": str(i)} for i in range(100)]
            return 200, {}, json.dumps(items).encode()

        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(handler))
        commits = client.list_commits(None, 300)
        self.assertEqual(len(commits), 100)
        self.assertTrue(any("repeated a page" in w for w in client.warnings))

    def test_status_pagination_uses_overall_state(self):
        def handler(url, headers):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            page = int(query["page"][0])
            if page == 1:
                body = {"state": "success", "statuses": [
                    {"context": "c%d" % i, "state": "success"} for i in range(100)]}
            elif page == 2:
                body = {"state": "failure", "statuses": [
                    {"context": "late", "state": "failure"}]}
            else:
                body = {"state": "failure", "statuses": []}
            return 200, {}, json.dumps(body).encode()

        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(handler))
        status = client.combined_status("sha")
        self.assertEqual(status["state"], "failure")
        self.assertEqual(len(status["statuses"]), 101)

    def test_commit_pagination(self):
        def handler(url, headers):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            page = int(query["page"][0])
            per = int(query["per_page"][0])
            total = 250
            start = (page - 1) * per
            items = [{"sha": str(i)} for i in range(start, min(start + per, total))]
            return 200, {"X-RateLimit-Remaining": "100"}, json.dumps(items).encode()

        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(handler))
        commits = client.list_commits(None, 300)
        self.assertEqual(len(commits), 250)
        self.assertEqual(commits[0]["sha"], "0")
        self.assertEqual(commits[-1]["sha"], "249")

    def test_check_run_pagination(self):
        def handler(url, headers):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            page = int(query["page"][0])
            if page == 1:
                body = {"total_count": 150, "check_runs": [check("a%d" % i) for i in range(100)]}
            else:
                body = {"total_count": 150, "check_runs": [check("b%d" % i) for i in range(50)]}
            return 200, {}, json.dumps(body).encode()

        client = ra.GitHubClient("o/r", api_base="https://example.test",
                                 transport=FakeTransport(handler))
        runs = client.check_runs("sha")
        self.assertEqual(len(runs), 150)


class CliTests(unittest.TestCase):
    def client(self, checks=None):
        commits = [commit("rev1", "This reverts commit a1b2c3d.")]
        details = {"a1b2c3d": commit("a1b2c3d", "Change", files=[{"filename": "a.py"}],
                                    stats={"additions": 3, "deletions": 1})}
        return FakeClient(commits=commits, details=details,
                          checks=checks if checks is not None else {"a1b2c3d": [check("ci")]})

    def test_invalid_repo(self):
        code, _ = run_main("not-a-repo", client=self.client())
        self.assertEqual(code, 2)

    def test_invalid_threshold(self):
        code, _ = run_main("o/r", "--fail-on-green-rate", "1.5", client=self.client())
        self.assertEqual(code, 2)

    def test_invalid_since(self):
        code, _ = run_main("o/r", "--since", "yesterday", client=self.client())
        self.assertEqual(code, 2)

    def test_offline_without_cache(self):
        code, _ = run_main("o/r", "--offline", "--no-cache")
        self.assertEqual(code, 2)

    def test_invalid_api_base(self):
        code, _ = run_main("o/r", "--api-base", "api.github.com", client=self.client())
        self.assertEqual(code, 2)

    def test_dot_segments_rejected(self):
        for repo in ("../..", "a/..", "./."):
            code, _ = run_main(repo, client=self.client())
            self.assertEqual(code, 2, repo)

    def test_negative_show(self):
        code, _ = run_main("o/r", "--show", "-1", client=self.client())
        self.assertEqual(code, 2)

    def test_human_median_does_not_truncate(self):
        commits = []
        details = {}
        for i, lines in enumerate((3, 4)):
            sha = "a%06x" % i
            commits.append(commit("d%06x" % i, "This reverts commit %s." % sha))
            details[sha] = commit(sha, "Change", files=[{"filename": "a.py"}],
                                  stats={"additions": lines, "deletions": 0})
        client = FakeClient(commits=commits, details=details,
                            checks={"a000000": [check("ci")], "a000001": [check("ci")]})
        code, out = run_main("o/r", client=client)
        self.assertEqual(code, 0)
        self.assertIn("3.5 lines", out)

    def test_json_report(self):
        code, out = run_main("o/r", "--json", client=self.client())
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["tool"], "revert-audit")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["summary"]["green_rate"], 1.0)
        self.assertEqual(len(payload["reverts"]), 1)

    def test_fail_on_green_rate(self):
        code, _ = run_main("o/r", "--fail-on-green-rate", "0.5", client=self.client())
        self.assertEqual(code, 1)

    def test_fail_on_green_rate_below_threshold(self):
        code, _ = run_main("o/r", "--fail-on-green-rate", "1.0", client=self.client(
            checks={"a1b2c3d": [check("ci", "failure")]}))
        self.assertEqual(code, 0)

    def test_fail_on_with_no_known(self):
        code, _ = run_main("o/r", "--fail-on-green-rate", "0.1", client=self.client(checks={}))
        self.assertEqual(code, 0)

    def test_human_output(self):
        code, out = run_main("o/r", client=self.client())
        self.assertEqual(code, 0)
        self.assertIn("CI AT THE TIME", out)
        self.assertIn("CI false-negative rate", out)
        self.assertIn("CAVEATS", out)

    def test_json_is_ascii_safe(self):
        client = self.client()
        client.details["a1b2c3d"]["commit"]["message"] = "Change 中文"
        code, out = run_main("o/r", "--json", client=client)
        self.assertEqual(code, 0)
        self.assertIn("\\u4e2d", out)


if __name__ == "__main__":
    unittest.main()
