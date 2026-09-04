#!/usr/bin/env bash
# Post every fixture payload to the local Lambda RIE and report pass/fail.
# Usage: bash tests/run_tests.sh
# Prerequisites: docker-compose up -d

set -euo pipefail

ENDPOINT="http://localhost:8080/2015-03-31/functions/function/invocations"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="${SCRIPT_DIR}/fixtures/payloads"

pass=0
fail=0

run_fixture() {
    local payload="$1"
    local expect_error="$2"   # "true" for failure fixtures
    local name
    name="$(basename "$(dirname "$payload")")"

    resp=$(curl -s -XPOST "$ENDPOINT" -d "@${payload}")

    if echo "$resp" | jq -e '.errorMessage' >/dev/null 2>&1; then
        # Lambda returned an unhandled exception
        if [ "$expect_error" = "true" ]; then
            echo "PASS (expected error): $name"
            pass=$((pass + 1))
        else
            echo "FAIL (unexpected error): $name"
            echo "  $resp" | head -5
            fail=$((fail + 1))
        fi
    else
        if [ "$expect_error" = "true" ]; then
            echo "FAIL (expected error, got success): $name"
            fail=$((fail + 1))
        else
            echo "PASS: $name"
            pass=$((pass + 1))
        fi
    fi
}

echo "=== success fixtures ==="
while IFS= read -r -d '' payload; do
    run_fixture "$payload" "false"
done < <(find "${FIXTURES_DIR}/success" -name "in.json" -print0 | sort -z)

echo ""
echo "=== failure fixtures ==="
while IFS= read -r -d '' payload; do
    run_fixture "$payload" "true"
done < <(find "${FIXTURES_DIR}/failure" -name "in.json" -print0 | sort -z)

echo ""
echo "Results: ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]
