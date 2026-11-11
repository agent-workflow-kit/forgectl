.PHONY: verify test golden lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

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
