.PHONY: verify test live golden lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

# Live suite against personal github.com and gitlab.com accounts (docs/live-tests.md). Never part
# of verify and never run in CI: it skips cleanly without the FORGECTL_LIVE_* settings.
live:
	uv run pytest -m live --run-live tests/live

golden:
	uv run pytest --update-golden

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .
