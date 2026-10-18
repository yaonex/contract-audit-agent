.PHONY: sync proto test eval audit index-kb lint

AGENT := uv run --project agent

sync:
	cd agent && uv sync --extra dev

proto:
	bash scripts/gen_proto.sh

test:
	$(AGENT) --extra dev pytest agent/tests

lint:
	$(AGENT) ruff check agent

audit:
	$(AGENT) audit-agent audit $(FILE)

index-kb:
	cd agent && uv run audit-agent index-kb ../knowledge_base/processed

eval:
	cd agent && uv run audit-agent eval --split dev

verify-model:
	@test -n "$(MODEL)" || (echo "usage: make verify-model MODEL=<model-name>"; exit 1)
	$(AGENT) python scripts/verify_model.py --model $(MODEL)
