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


# The compiler version to install: AUDIT_SOLC_VERSION from the environment, else from .env.
# (Named SOLC_TO_INSTALL so it never collides with the SOLC_VERSION variable solc-select reads.)
SOLC_TO_INSTALL ?= $(or $(AUDIT_SOLC_VERSION),$(shell sed -n 's/^AUDIT_SOLC_VERSION=//p' .env 2>/dev/null | tail -n 1))

# solc is a native binary, not a Python package, so it cannot live in uv.lock. solc-select
# keeps it (and the "current version") inside the project virtualenv: see slither_runner.py.
# Run again after the virtualenv is recreated.
setup-solc:
	@test -n "$(SOLC_TO_INSTALL)" || (echo "set AUDIT_SOLC_VERSION in .env (e.g. 0.8.34), or: make setup-solc SOLC_TO_INSTALL=0.8.34"; exit 1)
	$(AGENT) solc-select install $(SOLC_TO_INSTALL)
	$(AGENT) solc-select use $(SOLC_TO_INSTALL)
	$(AGENT) solc --version

check-tools:
	@echo "uv:      $$(uv --version)"
	@echo "slither: $$($(AGENT) slither --version)"
	@echo "solc:    $$($(AGENT) solc --version | tail -n 1)"

capture-fixtures:
	$(AGENT) bash scripts/capture_slither_fixtures.sh
