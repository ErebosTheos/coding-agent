# Quality Gates

This repo now has two gate levels:

1. `Local fast gate` (pre-push):
   - `python -m compileall -q src tests`
   - smoke imports for critical runtime modules
2. `CI strict gate`:
   - everything in fast gate
   - `pytest -q`

## Setup

```bash
python -m pip install -r requirements-dev.txt
pre-commit install --hook-type pre-commit --hook-type pre-push
```

## Run manually

```bash
make quality
make quality-ci
```

## Why this catches the failures you saw

- Missing module/import regressions are caught by smoke imports.
- Syntax and malformed generated edits are caught by `compileall`.
- Behavior regressions are caught by the full `pytest` CI run.
