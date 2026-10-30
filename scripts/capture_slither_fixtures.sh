#!/usr/bin/env bash
# Capture REAL Slither outputs as test fixtures (the format changes between versions, so the
# fixtures live in a directory named after the version). Run from the repo root:
#     make capture-fixtures
# Machine-specific paths are rewritten: repo root -> /ROOT, scratch dir -> /WORK, home -> /HOME.
# Review the result before committing.
set -u

ROOT="$(pwd)"
[ -f "$ROOT/samples/expected.yaml" ] || { echo "run this from the repo root"; exit 1; }
VERSION="$(slither --version | tr -d '[:space:]')"
MAIN_SOLC="$(solc --version | sed -n 's/^Version: \([0-9][0-9.]*\).*/\1/p')"
OUT="$ROOT/agent/tests/fixtures/slither-$VERSION"
WORK="$(mktemp -d)"
mkdir -p "$OUT"

# old_pragma needs compiler 0.8.19 in the PROJECT environment (the make target runs us inside it).
solc-select install 0.8.19 >/dev/null 2>&1 || echo "warning: could not install solc 0.8.19, the old_pragma capture will fail"

printf '// SPDX-License-Identifier: MIT\npragma solidity %s;\ncontract Empty {}\n' "$MAIN_SOLC" > "$WORK/Empty.sol"
printf '// SPDX-License-Identifier: MIT\npragma solidity 0.8.19;\ncontract Old {}\n' > "$WORK/Old.sol"
printf 'pragma solidity %s;\ncontract X { function f( }\n' "$MAIN_SOLC" > "$WORK/Broken.sol"

# capture <name> <target> [solc-version]
# Same flags as slither_runner.py. Saves the json if Slither wrote one, otherwise its stderr.
capture() {
  local name="$1" target="$2" solc="${3:-}"
  ( cd "$WORK" && env ${solc:+SOLC_VERSION=$solc} slither "$target" \
      --compile-force-framework solc --fail-none --json "$WORK/$name.json" \
      >/dev/null 2>"$WORK/$name.stderr" )
  local code=$?
  if [ -f "$WORK/$name.json" ]; then
    cp "$WORK/$name.json" "$OUT/$name.json"
  else
    cp "$WORK/$name.stderr" "$OUT/$name.stderr.txt"
  fi
  echo "$name: exit=$code json=$([ -f "$WORK/$name.json" ] && echo yes || echo no)"
}

capture bank_vulnerable   "$ROOT/samples/vulnerable/Bank.sol"
capture bank_safe         "$ROOT/samples/safe/Bank.sol"
capture vault             "$ROOT/samples/vulnerable/Vault.sol"
capture empty             "$WORK/Empty.sol"
capture old_pragma        "$WORK/Old.sol" 0.8.19      # solc-select 0.8.19 must be installed
capture version_mismatch  "$WORK/Old.sol"             # pragma 0.8.19 vs the global solc
capture syntax_error      "$WORK/Broken.sol"

slither --list-detectors > "$OUT/detectors.txt" 2>&1

sed -i "s#$ROOT#/ROOT#g; s#$WORK#/WORK#g; s#$HOME#/HOME#g" "$OUT"/*.json "$OUT"/*.txt 2>/dev/null
rm -rf "$WORK"
echo "written to $OUT (review, then git add)"