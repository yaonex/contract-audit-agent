#!/usr/bin/env bash
# Generate Python and Go gRPC code from proto/. Requires protoc-gen-go and protoc-gen-go-grpc for Go.
set -euo pipefail
cd "$(dirname "$0")/.."

# Python
python -m grpc_tools.protoc -Iproto \
  --python_out=agent/src/audit_agent/server \
  --grpc_python_out=agent/src/audit_agent/server \
  proto/audit/v1/audit.proto

# Go
protoc -Iproto \
  --go_out=server/gen --go_opt=paths=source_relative \
  --go-grpc_out=server/gen --go-grpc_opt=paths=source_relative \
  proto/audit/v1/audit.proto
