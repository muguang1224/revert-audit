---
name: revert-audit
description: Measure how often CI was green on changes that were later reverted. Use when a user asks how good their CI gate is, whether passing checks actually prevent bad merges, or wants a CI blind-spot metric for a GitHub repository.
---

# revert-audit

Quantifies one specific claim: **changes that had to be reverted often passed CI first**. It reads a GitHub repository's history and reports `green / (green + failed)` among resolved reverts.

## When to use

- "How good is our CI gate?" / "Do passing checks actually mean anything?"
- Comparing repositories or teams on CI false-negative rate.
- Before arguing for more tests, stricter required checks, or a different review process.
- Investigating a specific revert: `--json` shows the original commit, PR, check names, and failed checks.

## Run

```bash
python3 revert_audit.py owner/repo --limit 300 --max-reverts 20
GITHUB_TOKEN=... python3 revert_audit.py owner/repo --limit 1000 --json
```

Token from `--token`, `GITHUB_TOKEN`, or `GH_TOKEN`. Unauthenticated GitHub allows only 60 requests/hour; a 300-commit audit with reverts will exceed that.

## Reading the result

- `green`: every observed check run and legacy status passed. These are the blind spots.
- `failed`: at least one check failed at the time. CI did its job (for these).
- `unknown`: checks are missing, pending, or expired. Never count these as zero or as green.
- `re-land`: a revert of a revert. Excluded from the rate; counted separately.
- `unidentified`: the original change could not be resolved. Excluded from the rate.

## Guardrails

- **Do not say a reverted change was buggy.** Reverts are product decisions too. Use "reverted", never "defective".
- **Do not present a one-revert sample as a rate.** Require a reasonable `known` count; say the sample size out loud.
- **Do not treat missing checks as green.** `unknown` is the honest verdict; expired checks are common on old commits.
- **Do not compare repositories without stating the window.** `--limit`, `--since`, and default-branch activity change the denominator.
- **Do not edit the audited repository.** This is a read-only audit; propose changes separately.
- **Never commit the token or the cache.** The cache holds public API responses; the token is only sent in headers.

## Output contract

`--json` emits `schema_version: 1` with `repo`, `scanned`, `since`, `limit`, `truncated`, `reverts[]`, `summary`, and `warnings`. Each revert record has `revert_sha`, `original_sha`, `revert_date`, `original_date`, `pr_number`, `kind`, `relapse`, `unidentified`, `ci`, `ci_source`, `checks`, `failed_checks`, `checks_total`, `time_to_revert_days`, `files`, `additions`, `deletions`, `tests_touched`, `url`, and `reason`.

Exit codes: `0` report, `1` `--fail-on-green-rate` threshold met, `2` error.
