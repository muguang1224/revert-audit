# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog, and the project uses semantic versioning.

## [0.1.0] - 2026-10-18

### Added

- `revert-audit` CLI: measures `green / (green + failed)` among changes later
  reverted in a GitHub repository.
- Revert detection from `This reverts commit <sha>`, `Reverts #N`, squash-merge
  `Revert "..." (#N)` subjects, and revert-of-revert detection.
- Original resolution through commits and pull requests; PR head used for CI
  when available, merge commit as fallback.
- CI verdict from check runs and legacy commit statuses, with an explicit
  `unknown` state for missing/pending/unexpired checks.
- Metrics: green rate, median time to revert, median change size, tests-touched
  rate, top passing checks, revert density, duplicate/re-land/unidentified counts.
- GitHub REST client with ETag cache, `--offline`, `--refresh`, pagination,
  rate-limit diagnostics, and no third-party dependencies.
- 90 unittest cases with fake clients and transports; no network needed.
- `--fail-on-green-rate` for CI gating; stable JSON (`schema_version: 1`).

### Fixed during adversarial verification

Three independent reviewers attacked the API layer, the revert resolver, and
the metrics/CLI. The following real defects were fixed before publishing:

- A corrupt cache entry (`123`, missing `body`, `null`) could crash or, worse,
  produce a confident empty report; cache entries are now schema-checked.
- Cache-directory creation errors and scheme-less `--api-base` crashed with
  tracebacks; both now exit 2 with a message.
- Network read failures (`IncompleteRead`, SSL, resets) escaped as tracebacks.
- Pagination stopped on short pages (silent truncation behind proxies/GHES),
  could repeat pages, and changed `per_page` mid-stream; it now keeps page size
  constant, detects repeated pages, and stops only on an empty page.
- An empty 200 body was parsed as JSON `null` and cached as "no data".
- Squash-merged revert subjects (`Revert "X" (#124)`) were invisible.
- `Reverts other/repo#N` was treated as a local PR reference; `reverts #N` in an
  ordinary body was treated as a revert.
- One transient API error aborted the whole audit; per-commit errors now degrade
  to `unidentified` with a warning.
- Large merge commits lost file/line stats; PR `changed_files`/`additions`/
  `deletions` are now used when the commit has no file list.
- PR attribution picked an arbitrary merge when several PRs contained the
  commit; the earliest merged PR is used.
- `tests_touched` missed `app/tests.py`, `FooTest.php`, and `foo_spec.rb`.
- A PR title merely starting with "revert" (for example "Revert-aware merge
  queue") was misclassified as a re-land.
- Combined status ignored the API's aggregate `state` and was not paginated,
  which could turn a failure on a later page into a false "green".
- Repo validation accepted `.`/`..` path segments; `--show -1` had a misleading
  error; the human median truncated `3.5` to `3`.
