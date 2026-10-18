"""Shared helpers for the revert-audit test suite."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import revert_audit as ra  # noqa: E402


def commit(sha, message, date="2026-01-01T00:00:00Z", files=None, stats=None):
    return {
        "sha": sha,
        "html_url": "https://github.com/o/r/commit/" + sha,
        "commit": {
            "message": message,
            "author": {"date": date},
            "committer": {"date": date},
        },
        "files": files or [],
        "stats": stats or {},
    }


def pr(number, head_sha, merged_at="2026-01-02T00:00:00Z", title="Change"):
    return {
        "number": number,
        "title": title,
        "merged_at": merged_at,
        "merge_commit_sha": head_sha,
        "head": {"sha": head_sha},
    }


def check(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


class FakeClient:
    def __init__(self, commits=None, details=None, pulls_for=None, pulls=None,
                 checks=None, statuses=None, misses=None, errors=None,
                 commit_errors=None):
        self.commits = commits or []
        self.details = details or {}
        self.pulls_for = pulls_for or {}
        self.pulls = pulls or {}
        self.checks = checks or {}
        self.statuses = statuses or {}
        self.misses = misses or set()
        self.errors = errors or {}
        self.commit_errors = commit_errors or {}
        self.since = "__unset__"
        self.rate_remaining = None
        self.remaining = None

    def list_commits(self, since, limit):
        self.since = since
        return list(self.commits[:limit])

    def get_commit(self, sha):
        if sha in self.misses:
            raise ra.OfflineMiss("commit " + sha)
        if sha in self.commit_errors:
            raise self.commit_errors[sha]
        return self.details.get(sha)

    def pulls_for_commit(self, sha):
        if sha in self.misses:
            raise ra.OfflineMiss("pulls " + sha)
        return self.pulls_for.get(sha, [])

    def get_pull(self, number):
        return self.pulls.get(number)

    def check_runs(self, sha):
        if sha in self.errors:
            raise self.errors[sha]
        return self.checks.get(sha, [])

    def combined_status(self, sha):
        return self.statuses.get(sha, {})


def tmpdir(testcase):
    path = Path(tempfile.mkdtemp(prefix="revert-audit-test-"))
    testcase.addCleanup(shutil.rmtree, str(path), True)
    return path


def run_main(*args, client=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ra.main([str(a) for a in args],
                       client_factory=(lambda _args: client) if client else None)
    return code, buf.getvalue()
