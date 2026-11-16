.PHONY: dev test lint restore-test seed

dev:
	uv run --python 3.12 agent-board serve --reload

test:
	uv run --python 3.12 pytest -v

lint:
	uv run --python 3.12 ruff check src tests

restore-test:
	uv run --python 3.12 agent-board restore-test

seed:
	uv run --python 3.12 agent-board seed --demo
