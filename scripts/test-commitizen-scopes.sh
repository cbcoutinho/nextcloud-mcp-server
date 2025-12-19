#!/bin/bash
# Test commitizen scope filtering patterns
set -uo pipefail

echo "Testing commitizen scope filtering patterns..."
echo

# Regex patterns from configs
MCP_PATTERN='^(feat|fix|docs|refactor|perf|test|build|ci|chore)(\(mcp\))?(!)?:'
HELM_PATTERN='^(feat|fix|docs|refactor|perf|test|build|ci|chore)\(helm\)(!)?:'
ASTROLABE_PATTERN='^(feat|fix|docs|refactor|perf|test|build|ci|chore)\(astrolabe\)(!)?:'

test_pattern() {
    local message="$1"
    local pattern="$2"

    if echo "$message" | grep -qE "$pattern"; then
        return 0
    else
        return 1
    fi
}

run_test() {
    local message="$1"
    local expected="$2"
    local matched_components=()

    # Check which components match
    if test_pattern "$message" "$MCP_PATTERN"; then
        matched_components+=("mcp")
    fi
    if test_pattern "$message" "$HELM_PATTERN"; then
        matched_components+=("helm")
    fi
    if test_pattern "$message" "$ASTROLABE_PATTERN"; then
        matched_components+=("astrolabe")
    fi

    # Convert array to space-separated string, or "none" if empty
    local matched
    if [ ${#matched_components[@]} -eq 0 ]; then
        matched="none"
    else
        matched="${matched_components[*]}"
    fi

    # Validate expectation
    if [ "$matched" = "$expected" ]; then
        echo "✓ PASS: '$message'"
        echo "  → Matched: $matched"
        return 0
    else
        echo "✗ FAIL: '$message'"
        echo "  → Matched: $matched (expected: $expected)"
        return 1
    fi
}

# Run all test cases
failed=0
passed=0

# MCP server commits (scope=mcp or unscoped)
run_test "feat: add new feature" "mcp" && passed=$((passed+1)) || failed=$((failed+1))
run_test "feat(mcp): add API endpoint" "mcp" && passed=$((passed+1)) || failed=$((failed+1))
run_test "fix(mcp): resolve authentication bug" "mcp" && passed=$((passed+1)) || failed=$((failed+1))
run_test "docs: update README" "mcp" && passed=$((passed+1)) || failed=$((failed+1))

# Helm chart commits
run_test "feat(helm): add resource limits" "helm" && passed=$((passed+1)) || failed=$((failed+1))
run_test "fix(helm): correct values schema" "helm" && passed=$((passed+1)) || failed=$((failed+1))
run_test "docs(helm): update deployment guide" "helm" && passed=$((passed+1)) || failed=$((failed+1))

# Astrolabe commits
run_test "feat(astrolabe): add dark mode" "astrolabe" && passed=$((passed+1)) || failed=$((failed+1))
run_test "fix(astrolabe): resolve UI bug" "astrolabe" && passed=$((passed+1)) || failed=$((failed+1))
run_test "perf(astrolabe): optimize rendering" "astrolabe" && passed=$((passed+1)) || failed=$((failed+1))

# Breaking changes
run_test "feat(mcp)!: breaking API change" "mcp" && passed=$((passed+1)) || failed=$((failed+1))
run_test "feat(helm)!: rename values" "helm" && passed=$((passed+1)) || failed=$((failed+1))
run_test "feat(astrolabe)!: remove deprecated feature" "astrolabe" && passed=$((passed+1)) || failed=$((failed+1))

# Invalid commits (should not match any)
run_test "feat(invalid): test" "none" && passed=$((passed+1)) || failed=$((failed+1))
run_test "random commit message" "none" && passed=$((passed+1)) || failed=$((failed+1))
run_test "feat (mcp): space before scope" "none" && passed=$((passed+1)) || failed=$((failed+1))

# Summary
echo
echo "=========================================="
echo "Results: $passed passed, $failed failed"
echo "=========================================="

if [ $failed -gt 0 ]; then
    echo "❌ Some tests failed - scope patterns may need adjustment"
    exit 1
else
    echo "✅ All tests passed - scope patterns working correctly"
    exit 0
fi
