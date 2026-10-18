#!/usr/bin/env python3
"""revert-audit -- measure CI's blind spot on changes that were later reverted.

The tool walks a GitHub repository's commit history, finds reverts, resolves the
change each revert undid, and looks up the CI that ran on the original change.
The headline number is simple: of the changes that had to be reverted, how many
had passed every observed check at the time?

Zero dependencies. Uses the GitHub REST API, with an on-disk ETag cache,
optional offline mode, and no writes to the audited repository.

Exit codes:
  0  report produced (default)
  1  --fail-on-green-rate threshold was met
  2  tool/API error

Boundary: "green" means every check run and commit status observed by the API
passed. It does not mean the change was correct; a revert can be a product
decision. The report says "reverted", never "buggy".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__version__ = "0.1.0"
SCHEMA_VERSION = 1
DEFAULT_API = "https://api.github.com"

PASS_CONCLUSIONS = {"success", "neutral", "skipped"}
FAIL_CONCLUSIONS = {
    "failure", "timed_out", "cancelled", "action_required",
    "startup_failure", "stale",
}
PASS_STATUSES = {"success"}
FAIL_STATUSES = {"failure", "error"}

REVERT_SHA_RE = re.compile(r"this\s+reverts\s+commit\s+([0-9a-f]{7,40})", re.I)
REVERT_PR_RE = re.compile(
    r"reverts\s+((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?)#(\d+)", re.I)
REVERT_SUBJECT_RE = re.compile(
    r'^\s*revert\s+"(.*)"(?:\s*\(#\d+\))?\s*[.!]?\s*$', re.I)


def parse_revert_message(message: str, repo: str = "") -> Dict[str, Any]:
    """Extract the original change a commit message says it reverts."""
    text = message or ""
    sha_match = REVERT_SHA_RE.search(text)
    subject_line = _subject(text)
    subject_match = REVERT_SUBJECT_RE.match(subject_line)
    pr_match = REVERT_PR_RE.search(text)
    original_sha = sha_match.group(1).lower() if sha_match else None
    pr_repo = (pr_match.group(1).rstrip("#").lower() if pr_match and pr_match.group(1) else "")
    pr_number = None
    if pr_match and (sha_match or subject_match):
        # "reverts #12" in an ordinary commit body is not a revert reference.
        candidate = int(pr_match.group(2))
        if not pr_repo or not repo or pr_repo == repo.lower():
            pr_number = candidate
    is_revert = bool(original_sha or pr_number or subject_match)
    return {
        "is_revert": is_revert,
        "original_sha": original_sha,
        "pr_number": pr_number,
        "pr_repo": pr_repo,
        "subject_only": bool(subject_match and not original_sha and not pr_number),
        "subject": (subject_match.group(1) if subject_match else subject_line),
    }

TEST_PATTERNS = (
    re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|\.|$)", re.I),
    re.compile(r"(^|/)test_[^/]+\.py$", re.I),
    re.compile(r"_test\.py$", re.I),
    re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$", re.I),
    re.compile(r"_test\.go$", re.I),
    re.compile(r"(Test|Tests|IT)\.(java|kt|cs|php)$"),
    re.compile(r"_spec\.rb$", re.I),
    re.compile(r"_test\.rb$", re.I),
    re.compile(r"(^|/)testdata/", re.I),
)


class ToolError(Exception):
    pass


class ApiError(ToolError):
    def __init__(self, status: int, message: str, url: str = ""):
        self.status = status
        self.url = url
        super().__init__(message)


class AuthError(ApiError):
    pass


class RateLimitError(ApiError):
    def __init__(self, reset: Optional[int], message: str):
        self.reset = reset
        super().__init__(403, message)


class NotFound(ApiError):
    def __init__(self, url: str):
        super().__init__(404, "not found: %s" % url, url)


class OfflineMiss(ToolError):
    def __init__(self, url: str):
        self.url = url
        super().__init__("offline cache miss: %s" % url)


# ---------------------------------------------------------------------------
# HTTP + cache


def _header(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    try:
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
    except AttributeError:
        pass
    return None


class HttpTransport:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def request(self, url: str, headers: Dict[str, str]) -> Tuple[int, Any, bytes]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except urllib.error.URLError as exc:
            raise ToolError("network error: %s (%s)" % (exc, url))
        except Exception as exc:
            raise ToolError("network error while reading %s: %s" % (url, exc))


class Cache:
    def __init__(self, directory: Path):
        self.directory = directory
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError("could not create cache directory %s: %s" % (directory, exc))

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / (key + ".json")

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or "body" not in payload:
            return None
        if payload.get("body") is None:
            return None
        return payload

    def save(self, key: str, etag: Optional[str], body: Any) -> None:
        path = self._path(key)
        tmp = path.with_suffix(".tmp")
        payload = {"etag": etag, "body": body}
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


class GitHubClient:
    def __init__(self, repo: str, token: str = "", api_base: str = DEFAULT_API,
                 cache_dir: Optional[Path] = None, offline: bool = False,
                 refresh: bool = False, transport: Optional[Any] = None):
        self.repo = repo
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.offline = offline
        self.refresh = refresh
        self.transport = transport or HttpTransport()
        self.cache = Cache(cache_dir) if cache_dir else None
        self.remaining: Optional[int] = None
        self.reset: Optional[int] = None
        self.warnings: List[str] = []

    def _url(self, path: str, params: Optional[Dict[str, Any]]) -> str:
        url = self.api_base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(sorted(clean.items()))
        return url

    def _record_rate(self, headers: Any) -> None:
        remaining = _header(headers, "X-RateLimit-Remaining")
        reset = _header(headers, "X-RateLimit-Reset")
        if remaining is not None:
            try:
                self.remaining = int(remaining)
            except ValueError:
                pass
        if reset is not None:
            try:
                self.reset = int(reset)
            except ValueError:
                pass

    def get(self, path: str, params: Optional[Dict[str, Any]] = None,
            allow_404: bool = False) -> Any:
        url = self._url(path, params)
        key = Cache.key(url)
        cached = self.cache.load(key) if self.cache else None
        if self.offline:
            if cached is None:
                raise OfflineMiss(url)
            return cached.get("body")

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "revert-audit/" + __version__,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if cached and not self.refresh and cached.get("etag"):
            headers["If-None-Match"] = str(cached["etag"])

        status, raw_headers, raw_body = self.transport.request(url, headers)
        self._record_rate(raw_headers)
        if status == 304 and cached is not None:
            return cached.get("body")
        if status == 200:
            if not raw_body:
                raise ApiError(status, "empty response body", url)
            try:
                body = json.loads(raw_body.decode("utf-8") or "null")
            except (UnicodeDecodeError, ValueError) as exc:
                raise ApiError(status, "invalid JSON from GitHub: %s" % exc, url)
            if self.cache:
                self.cache.save(key, _header(raw_headers, "ETag"), body)
            return body
        if status == 404:
            if allow_404:
                return None
            raise NotFound(url)
        message = _github_message(raw_body)
        if status in (401,):
            raise AuthError(status, "authentication failed: %s" % message, url)
        if status in (403, 429):
            remaining = _header(raw_headers, "X-RateLimit-Remaining")
            if remaining == "0" or "rate limit" in message.lower():
                reset = None
                raw_reset = _header(raw_headers, "X-RateLimit-Reset")
                if raw_reset and raw_reset.isdigit():
                    reset = int(raw_reset)
                raise RateLimitError(reset, "GitHub rate limit exhausted: %s" % message)
            raise ApiError(status, "forbidden: %s" % message, url)
        raise ApiError(status, "GitHub API error %d: %s" % (status, message), url)

    def _paginate(self, path: str, params: Dict[str, Any], limit: int) -> List[Any]:
        out: List[Any] = []
        page = 1
        seen_pages: Set[Tuple[str, str, int]] = set()
        while len(out) < limit and page <= 1000:
            query = dict(params)
            query["page"] = page
            # Keep per_page constant: page * per_page only stays aligned when
            # the page size does not shrink on the final page.
            query["per_page"] = 100
            data = self.get(path, query)
            if not isinstance(data, list) or not data:
                break
            if data and isinstance(data[0], dict):
                signature = (str(data[0].get("sha")), str(data[-1].get("sha")), len(data))
                if signature in seen_pages:
                    self.warnings.append("pagination repeated a page; stopped at page %d" % page)
                    break
                seen_pages.add(signature)
            out.extend(data)
            # Do not stop on a short page: some proxies/GHES cap per_page lower
            # than requested. The next page is the only reliable signal.
            page += 1
        if page > 1000:
            self.warnings.append("pagination stopped after 1000 pages")
        return out[:limit]

    # -- API surface used by the analyzer ---------------------------------

    def list_commits(self, since: Optional[str], limit: int) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if since:
            params["since"] = since
        return self._paginate("/repos/%s/commits" % self.repo, params, limit)

    def get_commit(self, sha: str) -> Optional[Dict[str, Any]]:
        return self.get("/repos/%s/commits/%s" % (self.repo, sha), allow_404=True)

    def pulls_for_commit(self, sha: str) -> List[Dict[str, Any]]:
        data = self.get("/repos/%s/commits/%s/pulls" % (self.repo, sha),
                        {"per_page": 100}, allow_404=True)
        return data if isinstance(data, list) else []

    def get_pull(self, number: int) -> Optional[Dict[str, Any]]:
        return self.get("/repos/%s/pulls/%d" % (self.repo, number), allow_404=True)

    def check_runs(self, sha: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page = 1
        total: Optional[int] = None
        while page <= 1000:
            data = self.get("/repos/%s/commits/%s/check-runs" % (self.repo, sha),
                            {"per_page": 100, "page": page}, allow_404=True)
            if not data:
                break
            runs = data.get("check_runs") or []
            if not runs:
                break
            out.extend(runs)
            if isinstance(data.get("total_count"), int):
                total = int(data["total_count"])
            if total is not None and len(out) >= total:
                break
            page += 1
        return out

    def combined_status(self, sha: str) -> Dict[str, Any]:
        statuses: List[Dict[str, Any]] = []
        overall = ""
        rank = {"": 0, "success": 1, "pending": 2, "failure": 3, "error": 3}
        page = 1
        while page <= 100:
            data = self.get("/repos/%s/commits/%s/status" % (self.repo, sha),
                            {"per_page": 100, "page": page}, allow_404=True)
            if not data:
                break
            state = str(data.get("state") or "").lower()
            if rank.get(state, 0) > rank.get(overall, 0):
                overall = state
            batch = data.get("statuses") or []
            if not batch:
                break
            statuses.extend(batch)
            page += 1
        return {"state": overall, "statuses": statuses}


def _github_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
        if isinstance(payload, dict) and payload.get("message"):
            return str(payload["message"])
    except (UnicodeDecodeError, ValueError):
        pass
    return (raw or b"").decode("utf-8", "replace")[:300]


# ---------------------------------------------------------------------------
# revert parsing + CI classification


def _subject(message: str) -> str:
    return (message or "").strip().splitlines()[0] if (message or "").strip() else ""


def _date(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("Z", "+00:00")


def _days_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (b - a).total_seconds() / 86400.0
    if delta < 0:
        return None
    return round(delta, 2)


def _commit_date(commit: Optional[Dict[str, Any]]) -> Optional[str]:
    if not commit:
        return None
    inner = commit.get("commit") or {}
    for key in ("committer", "author"):
        value = (inner.get(key) or {}).get("date")
        if value:
            return _date(value)
    return None


def _tests_touched(files: Sequence[Dict[str, Any]]) -> bool:
    for entry in files:
        name = entry.get("filename") or ""
        if any(pattern.search(name) for pattern in TEST_PATTERNS):
            return True
    return False


@dataclass
class CiResult:
    state: str                 # green | failed | unknown
    source: str = ""
    checks: List[str] = field(default_factory=list)
    failed_checks: List[str] = field(default_factory=list)
    total: int = 0
    reason: str = ""


def classify_ci(runs: Sequence[Dict[str, Any]],
                status: Dict[str, Any]) -> CiResult:
    """Combine check runs and legacy commit statuses into one verdict."""
    names: List[str] = []
    failed: List[str] = []
    passed = 0
    pending = 0
    for run in runs:
        name = str(run.get("name") or "check")
        names.append(name)
        if str(run.get("status") or "").lower() != "completed":
            pending += 1
            continue
        conclusion = str(run.get("conclusion") or "").lower()
        if conclusion in FAIL_CONCLUSIONS:
            failed.append(name)
        elif conclusion in PASS_CONCLUSIONS:
            passed += 1
        else:
            pending += 1
    for item in status.get("statuses") or []:
        context = str(item.get("context") or "status")
        if context not in names:
            names.append(context)
        state = str(item.get("state") or "").lower()
        if state in FAIL_STATUSES:
            failed.append(context)
        elif state in PASS_STATUSES:
            passed += 1
        elif state in ("pending", ""):
            pending += 1
    overall = str(status.get("state") or "").lower()
    if overall in FAIL_STATUSES and not failed:
        names.append("combined-status")
        failed.append("combined-status")
    elif overall in PASS_STATUSES and not names:
        names.append("combined-status")
        passed = 1
    elif overall == "pending" and not pending:
        pending = 1
    total = len(names)
    if failed:
        return CiResult("failed", checks=names, failed_checks=failed, total=total)
    if total == 0:
        return CiResult("unknown", checks=[], failed_checks=[], total=0,
                        reason="no checks or statuses")
    if pending:
        return CiResult("unknown", checks=names, failed_checks=[], total=total,
                        reason="checks still pending")
    return CiResult("green", checks=names, failed_checks=[], total=total)


def evaluate_ci(client: Any, candidates: Sequence[str]) -> CiResult:
    """Try each candidate SHA (PR head, then merge commit) until definitive."""
    last = CiResult("unknown", reason="no candidate commit")
    seen: Set[str] = set()
    for sha in candidates:
        if not sha or sha in seen:
            continue
        seen.add(sha)
        try:
            runs = client.check_runs(sha)
            status = client.combined_status(sha)
        except OfflineMiss:
            last = CiResult("unknown", source=sha, reason="offline cache miss")
            continue
        except RateLimitError:
            raise
        except AuthError:
            raise
        except ApiError as exc:
            last = CiResult("unknown", source=sha,
                            reason="checks unavailable (%d)" % exc.status)
            continue
        result = classify_ci(runs or [], status or {})
        result.source = sha
        if result.state in ("green", "failed"):
            return result
        last = result
    return last


# ---------------------------------------------------------------------------
# analysis


@dataclass
class Record:
    revert_sha: str
    revert_date: Optional[str]
    revert_subject: str
    original_sha: Optional[str]
    original_subject: str
    original_date: Optional[str]
    pr_number: Optional[int]
    kind: str
    relapse: bool
    unidentified: bool
    ci: str
    ci_source: str
    checks: List[str]
    failed_checks: List[str]
    checks_total: int
    time_to_revert_days: Optional[float]
    files: int
    additions: int
    deletions: int
    tests_touched: bool
    url: str
    reason: str = ""


@dataclass
class Report:
    repo: str
    scanned: int
    since: Optional[str]
    limit: int
    truncated: bool
    records: List[Record] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    duplicates: int = 0
    revert_commits: int = 0


def _resolve_original(client: Any, parsed: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    commit = None
    pull = None
    if parsed.get("original_sha"):
        commit = client.get_commit(parsed["original_sha"])
    if commit is None and parsed.get("pr_number"):
        pull = client.get_pull(parsed["pr_number"])
        if pull:
            sha = pull.get("merge_commit_sha") or ((pull.get("head") or {}).get("sha"))
            if sha:
                commit = client.get_commit(sha)
    return commit, pull


def _pull_for_commit(client: Any, commit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sha = commit.get("sha")
    if not sha:
        return None
    pulls = client.pulls_for_commit(sha)
    merged = [p for p in pulls if p.get("merged_at")]
    if merged:
        return min(merged, key=lambda p: str(p.get("merged_at")))
    return pulls[0] if pulls else None


def _make_record(client: Any, commit: Dict[str, Any], parsed: Dict[str, Any],
                 original: Optional[Dict[str, Any]], pull: Optional[Dict[str, Any]]) -> Record:
    revert_sha = str(commit.get("sha") or "")
    revert_date = _commit_date(commit)
    revert_subject = _subject((commit.get("commit") or {}).get("message", ""))
    url = commit.get("html_url") or ""
    if not original:
        kind = ("commit" if parsed.get("original_sha")
                else "pull" if parsed.get("pr_number") else "subject-only")
        return Record(
            revert_sha=revert_sha, revert_date=revert_date,
            revert_subject=revert_subject, original_sha=parsed.get("original_sha"),
            original_subject="", original_date=None,
            pr_number=parsed.get("pr_number"), kind=kind,
            relapse=False, unidentified=True, ci="unknown", ci_source="",
            checks=[], failed_checks=[], checks_total=0,
            time_to_revert_days=None, files=0, additions=0, deletions=0,
            tests_touched=False, url=url, reason="original change not resolved",
        )
    original_sha = str(original.get("sha") or "")
    original_message = (original.get("commit") or {}).get("message", "")
    original_subject = _subject(original_message)
    if pull is None and parsed.get("pr_number"):
        try:
            pull = client.get_pull(parsed["pr_number"])
        except OfflineMiss:
            pull = None
    if pull is None:
        try:
            pull = _pull_for_commit(client, original)
        except OfflineMiss:
            pull = None
    original_date = _date((pull or {}).get("merged_at")) or _commit_date(original)
    pr_number = int(pull["number"]) if pull and pull.get("number") is not None else parsed.get("pr_number")
    relapse = bool(parse_revert_message(original_message).get("is_revert"))
    if pull and re.match(r'^\s*revert\s+"', str(pull.get("title") or ""), re.I):
        relapse = True

    head_sha = ((pull or {}).get("head") or {}).get("sha")
    candidates = [head_sha, original_sha]
    if relapse:
        ci = CiResult("skipped", reason="revert of a revert (re-land)")
    else:
        ci = evaluate_ci(client, candidates)
    files = original.get("files") or []
    stats = original.get("stats") or {}
    additions = int(stats.get("additions") or 0)
    deletions = int(stats.get("deletions") or 0)
    file_count = len(files)
    if not stats and files:
        additions = sum(int(f.get("additions") or 0) for f in files)
        deletions = sum(int(f.get("deletions") or 0) for f in files)
    if not files and pull:
        additions = int(pull.get("additions") or additions)
        deletions = int(pull.get("deletions") or deletions)
        file_count = int(pull.get("changed_files") or 0)
    return Record(
        revert_sha=revert_sha, revert_date=revert_date,
        revert_subject=revert_subject, original_sha=original_sha,
        original_subject=original_subject, original_date=original_date,
        pr_number=pr_number, kind="pull" if parsed.get("pr_number") else "commit",
        relapse=relapse, unidentified=False,
        ci="skipped" if relapse else ci.state,
        ci_source=ci.source, checks=ci.checks, failed_checks=ci.failed_checks,
        checks_total=ci.total,
        time_to_revert_days=_days_between(original_date, revert_date),
        files=file_count, additions=additions, deletions=deletions,
        tests_touched=_tests_touched(files), url=url,
        reason="revert of a revert (re-land)" if relapse else ci.reason,
    )


def analyze(repo: str, client: Any, limit: int = 300, since: Optional[str] = None,
            max_reverts: int = 50) -> Report:
    report = Report(repo=repo, scanned=0, since=since, limit=limit, truncated=False)
    commits = client.list_commits(since=since, limit=limit)
    report.warnings.extend(getattr(client, "warnings", []))
    examined = 0
    seen: Set[str] = set()
    for commit in commits:
        examined += 1
        message = (commit.get("commit") or {}).get("message", "")
        parsed = parse_revert_message(message, repo)
        if not parsed.get("is_revert"):
            continue
        if len(report.records) >= max_reverts:
            report.truncated = True
            break
        report.revert_commits += 1
        try:
            original, pull = _resolve_original(client, parsed)
            record = _make_record(client, commit, parsed, original, pull)
        except (RateLimitError, AuthError):
            raise
        except ApiError as exc:
            report.warnings.append("could not resolve a revert: %s" % exc)
            record = _make_record(client, commit, parsed, None, None)
            record.reason = "API error while resolving (%d)" % exc.status
        except OfflineMiss as exc:
            report.warnings.append(str(exc))
            record = _make_record(client, commit, parsed, None, None)
            record.reason = "offline cache miss while resolving"
        key = (record.original_sha or record.revert_sha).lower()
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)
        report.records.append(record)
    report.scanned = examined if report.truncated else len(commits)
    if len(commits) >= limit:
        report.truncated = True
    return report


# ---------------------------------------------------------------------------
# summary + rendering


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(statistics.median(values)), 2)


def summarize(report: Report) -> Dict[str, Any]:
    records = report.records
    relapses = sum(1 for r in records if r.relapse)
    unidentified = sum(1 for r in records if r.unidentified)
    analyzed = [r for r in records if not r.relapse and not r.unidentified]
    green = [r for r in analyzed if r.ci == "green"]
    failed = [r for r in analyzed if r.ci == "failed"]
    unknown = [r for r in analyzed if r.ci == "unknown"]
    known = len(green) + len(failed)
    green_rate = round(len(green) / known, 4) if known else None
    check_counts: Dict[str, int] = {}
    for record in green:
        for name in record.checks:
            check_counts[name] = check_counts.get(name, 0) + 1
    failed_counts: Dict[str, int] = {}
    for record in failed:
        for name in record.failed_checks:
            failed_counts[name] = failed_counts.get(name, 0) + 1
    with_files = [r for r in analyzed if r.files]
    return {
        "total": len(records),
        "relapses": relapses,
        "unidentified": unidentified,
        "analyzed": len(analyzed),
        "green": len(green),
        "failed": len(failed),
        "unknown": len(unknown),
        "known": known,
        "green_rate": green_rate,
        "median_time_to_revert_days": _median(
            [r.time_to_revert_days for r in analyzed if r.time_to_revert_days is not None]),
        "median_change_lines": _median(
            [float(r.additions + r.deletions) for r in with_files]),
        "files_known": len(with_files),
        "tests_touched": sum(1 for r in with_files if r.tests_touched),
        "tests_touched_rate": round(
            sum(1 for r in with_files if r.tests_touched) / len(with_files), 4)
        if with_files else None,
        "top_green_checks": sorted(check_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "top_failed_checks": sorted(failed_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "duplicates": report.duplicates,
        "revert_commits": report.revert_commits,
        "reverts_per_100_commits": round(
            len(records) / report.scanned * 100, 2) if report.scanned else None,
    }


def _short(sha: Optional[str], n: int = 7) -> str:
    return (sha or "-")[:n]


def _fmt_date(value: Optional[str]) -> str:
    if not value:
        return "-"
    return value[:10]


def _color(text: str, code: str, enabled: bool) -> str:
    return "\x1b[%sm%s\x1b[0m" % (code, text) if enabled else text


def render_human(report: Report, summary: Dict[str, Any], show: int = 20) -> str:
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    out: List[str] = []
    out.append("revert-audit %s -- %s" % (__version__, report.repo))
    out.append("scanned %d commits%s%s" % (
        report.scanned,
        " since " + report.since if report.since else "",
        " (truncated)" if report.truncated else "",
    ))
    out.append("")
    out.append("REVERTS")
    out.append("  found %d commits; %d unique changes; %d duplicates; %d re-lands; %d unidentified"
               % (summary["revert_commits"], summary["total"], summary["duplicates"],
                  summary["relapses"], summary["unidentified"]))
    out.append("")
    out.append("CI AT THE TIME OF THE ORIGINAL CHANGE")
    known = summary["known"]
    if known:
        out.append("  %s %4d   %5.1f%% of known"
                   % (_color("green".ljust(7), "32", color), summary["green"],
                      100.0 * summary["green"] / known))
        out.append("  %s %4d   %5.1f%% of known"
                   % (_color("failed".ljust(7), "31", color), summary["failed"],
                      100.0 * summary["failed"] / known))
    else:
        out.append("  green       0")
        out.append("  failed      0")
    out.append("  unknown  %4d" % summary["unknown"])
    if summary["green_rate"] is not None:
        out.append("")
        out.append("  CI false-negative rate on reverted changes: %.1f%%"
                   % (100.0 * summary["green_rate"]))
    out.append("")
    out.append("  median time to revert: %s days"
               % ("-" if summary["median_time_to_revert_days"] is None
                  else summary["median_time_to_revert_days"]))
    out.append("  median change size:    %s lines"
               % ("-" if summary["median_change_lines"] is None
                  else ("%g" % summary["median_change_lines"])))
    if summary["tests_touched_rate"] is not None:
        out.append("  tests touched:         %d/%d (%.1f%%)"
                   % (summary["tests_touched"], summary["files_known"],
                      100.0 * summary["tests_touched_rate"]))
    if summary["top_green_checks"]:
        out.append("")
        out.append("CHECKS THAT PASSED ON GREEN REVERTS")
        for name, count in summary["top_green_checks"]:
            out.append("  %-45s %d" % (name[:45], count))
    out.append("")
    out.append("REVERTS (newest first)")
    out.append("  %-8s %-8s %-10s %-7s %-6s %s" %
               ("REVERT", "ORIGINAL", "REVERTED", "CI", "FILES", "SUBJECT"))
    for record in report.records[:show]:
        ci = record.ci
        if ci == "green":
            ci = _color(ci.ljust(7), "32", color)
        elif ci == "failed":
            ci = _color(ci.ljust(7), "31", color)
        elif ci == "skipped":
            ci = _color("re-land".ljust(7), "36", color)
        else:
            ci = ci.ljust(7)
        out.append("  %-8s %-8s %-10s %s %-6s %s" % (
            _short(record.revert_sha), _short(record.original_sha),
            _fmt_date(record.revert_date), ci, record.files,
            (record.revert_subject or record.original_subject or "-")[:60]))
    if len(report.records) > show:
        out.append("  ... %d more (use --json for all)" % (len(report.records) - show))
    if report.warnings:
        out.append("")
        out.append("NOTES")
        for warning in report.warnings[:10]:
            out.append("  - %s" % warning)
    out.append("")
    out.append("CAVEATS")
    out.append("  'green' = every check run and commit status observed on the original")
    out.append("  change passed. It does not prove the change was correct, and a revert")
    out.append("  can be a product decision. Old commits may have no checks left.")
    return "\n".join(out)


def render_json(report: Report, summary: Dict[str, Any]) -> str:
    payload = {
        "tool": "revert-audit",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "repo": report.repo,
        "scanned": report.scanned,
        "since": report.since,
        "limit": report.limit,
        "truncated": report.truncated,
        "reverts": [asdict(record) for record in report.records],
        "summary": summary,
        "warnings": report.warnings,
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)


# ---------------------------------------------------------------------------
# CLI


def _repo_ok(value: str) -> bool:
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", value or ""):
        return False
    owner, name = value.split("/", 1)
    return owner not in (".", "..") and name not in (".", "..")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revert-audit",
        description="Measure how often CI was green on changes that were later reverted.",
    )
    parser.add_argument("repo", help="GitHub repository as owner/name")
    parser.add_argument("--token", default=None,
                        help="GitHub token (default: GITHUB_TOKEN or GH_TOKEN)")
    parser.add_argument("--api-base", default=DEFAULT_API, help="GitHub API base URL")
    parser.add_argument("--since", default=None, help="only commits after this ISO date")
    parser.add_argument("--limit", type=int, default=300, help="commit scan limit (default: 300)")
    parser.add_argument("--max-reverts", type=int, default=50,
                        help="stop after this many unique reverts (default: 50)")
    parser.add_argument("--cache-dir", type=Path, default=Path(".revert-audit-cache"),
                        help="ETag cache directory (default: .revert-audit-cache)")
    parser.add_argument("--no-cache", action="store_true", help="disable the on-disk cache")
    parser.add_argument("--offline", action="store_true", help="only read from the cache")
    parser.add_argument("--refresh", action="store_true", help="ignore cached ETags")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--show", type=int, default=20, help="rows in the human table (default: 20)")
    parser.add_argument("--fail-on-green-rate", type=float, default=None, metavar="RATE",
                        help="exit 1 when the green rate is at least RATE (0..1)")
    parser.add_argument("--version", action="version", version="revert-audit " + __version__)
    return parser


def main(argv: Optional[Sequence[str]] = None, client_factory: Optional[Any] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not _repo_ok(args.repo):
        print("revert-audit: error: repo must be owner/name", file=sys.stderr)
        return 2
    if args.limit < 1 or args.max_reverts < 1 or args.show < 0:
        print("revert-audit: error: --limit/--max-reverts must be >= 1 and --show >= 0",
              file=sys.stderr)
        return 2
    if args.fail_on_green_rate is not None and not (0.0 <= args.fail_on_green_rate <= 1.0):
        print("revert-audit: error: --fail-on-green-rate must be between 0 and 1", file=sys.stderr)
        return 2
    if args.since:
        try:
            datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            print("revert-audit: error: --since must be an ISO date", file=sys.stderr)
            return 2
    if not re.match(r"^https?://", args.api_base or ""):
        print("revert-audit: error: --api-base must start with http:// or https://",
              file=sys.stderr)
        return 2
    if args.offline and args.no_cache:
        print("revert-audit: error: --offline requires the cache", file=sys.stderr)
        return 2

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    try:
        if client_factory is not None:
            client = client_factory(args)
        else:
            cache_dir = None if args.no_cache else args.cache_dir
            if args.offline and cache_dir is None:
                print("revert-audit: error: --offline requires the cache", file=sys.stderr)
                return 2
            client = GitHubClient(
                args.repo, token=token, api_base=args.api_base,
                cache_dir=cache_dir, offline=args.offline, refresh=args.refresh,
            )
    except ToolError as exc:
        print("revert-audit: error: %s" % exc, file=sys.stderr)
        return 2
    if not token and client_factory is None:
        estimate = (args.limit + 99) // 100 + args.max_reverts * 4
        print("revert-audit: note: no token; unauthenticated GitHub allows 60 requests/hour"
              " (this run may need ~%d)" % estimate, file=sys.stderr)

    try:
        report = analyze(args.repo, client, limit=args.limit, since=args.since,
                         max_reverts=args.max_reverts)
    except RateLimitError as exc:
        reset = ""
        if exc.reset:
            reset = " (resets at %s UTC)" % time.strftime(
                "%Y-%m-%d %H:%M", time.gmtime(exc.reset))
        print("revert-audit: error: %s%s" % (exc, reset), file=sys.stderr)
        return 2
    except (AuthError, ApiError, OfflineMiss, ToolError) as exc:
        print("revert-audit: error: %s" % exc, file=sys.stderr)
        return 2

    if getattr(client, "remaining", None) is not None and client.remaining < 20:
        report.warnings.append("GitHub rate limit remaining: %s" % client.remaining)
    summary = summarize(report)
    if args.json:
        _emit(render_json(report, summary))
    else:
        _emit(render_human(report, summary, show=args.show))

    if args.fail_on_green_rate is not None:
        rate = summary["green_rate"]
        if rate is not None and rate >= args.fail_on_green_rate:
            return 1
    return 0


def _emit(text: str) -> None:
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.write(text.encode(encoding, "backslashreplace").decode(encoding) + "\n")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
