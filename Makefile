.PHONY: setup proto test eval audit index-kb lint

setup:
	cd agent && uv sync

proto:
	bash scripts/gen_proto.sh

test:
	cd agent && uv run pytest -q

lint:
	cd agent && uv run ruff check src tests

audit:
	cd agent && uv run audit-agent audit ../$(FILE) --out ../reports

index-kb:
	cd agent && uv run audit-agent index-kb ../knowledge_base/processed

eval:
	cd agent && uv run audit-agent eval --split dev
