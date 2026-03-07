.PHONY: quality quality-ci hooks

quality:
	python scripts/quality_gate.py --mode fast

quality-ci:
	python scripts/quality_gate.py --mode ci

hooks:
	pre-commit install --hook-type pre-commit --hook-type pre-push
