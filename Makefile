.PHONY: install check format test test-integration push dry-run train-openapi delete-content logs build-schema

install:
	uv sync --locked

check:
	uv run --locked black --check cli workflows conftest.py .githooks/pre-commit
	uv run --locked flake8 cli workflows conftest.py .githooks/pre-commit
	uv run --locked pytest

format:
	uv run --locked black cli workflows conftest.py .githooks/pre-commit

test:
	uv run --locked pytest

test-integration:
	uv run --locked pytest -m integration

push:
	uv run --locked python cli/flow.py push

dry-run:
	uv run --locked python cli/flow.py push --dry-run

train-openapi:
	uv run --locked python -m cli.operations train-openapi $(ARGS)

delete-content:
	uv run --locked python -m cli.operations delete-content $(ARGS)

logs:
	uv run --locked python -m cli.operations logs $(ARGS)

build-schema:
	uv run --locked python -m cli.schema_refresh $(ARGS)

.PHONY: push-dynamic dry-run-dynamic

push-dynamic:
	uv run --locked python cli/flow.py push-dynamic $(ARGS)

dry-run-dynamic:
	uv run --locked python cli/flow.py push-dynamic --dry-run $(ARGS)
