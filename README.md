# Blast Radius entry

Selects sqlglot tests for each patch from a coverage map of the pinned commit.

- `map.json.gz`: built from the pinned sqlglot commit (eb11aec4) by running its test suite
  with per-test line coverage (process pools run in-process so their work is traced) and
  recording which data files each test reads. No patch-specific answers.
- `selector.py`: for each patch, finds changed lines (and the whole statement around them),
  unions the tests that executed them; import-time lines fall back to every test of that
  dialect family (or all tests for core files); fixture edits select the tests that read
  the fixture; comment-only Python edits select nothing; anything unrecognized runs all.
  Tests are split across 1–8 workers to minimise busiest load + 2 s per worker.

## Dependencies
- `sqlglot-src.tar.gz`: the `sqlglot/` directory of https://github.com/tobymao/sqlglot at
  commit eb11aec4be8cb2122afe713a4816662c3f0fb2fa (MIT License).
- Base image `python:3.13-slim` (pinned digest). Standard library only.

MIT License (see LICENSE).
