#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../agent"
uv run audit-agent index-kb ../knowledge_base/processed
