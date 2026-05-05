#!/usr/bin/env sh
set -eu

run_with_optional_datadog() {
  if [ -n "${APP_BUILD_VERSION:-}" ] && [ -z "${DD_VERSION:-}" ]; then
    export DD_VERSION="$APP_BUILD_VERSION"
  fi

  DATADOG_RUN_ENABLED="${DATADOG_RUN_ENABLED:-${DD_TRACE_ENABLED:-}}"
  if [ -z "$DATADOG_RUN_ENABLED" ]; then
    if [ -n "${DD_AGENT_HOST:-}" ] \
      || [ -n "${DD_TRACE_AGENT_URL:-}" ] \
      || [ -n "${DD_PROFILING_ENABLED:-}" ] \
      || [ -n "${DD_DYNAMIC_INSTRUMENTATION_ENABLED:-}" ] \
      || [ -n "${DD_LLMOBS_ENABLED:-}" ]; then
      DATADOG_RUN_ENABLED=true
    else
      DATADOG_RUN_ENABLED=false
    fi
  fi

  case "$DATADOG_RUN_ENABLED" in
    true | 1 | yes | on)
      exec .venv/bin/ddtrace-run .venv/bin/python "$@"
      ;;
    *)
      exec .venv/bin/python "$@"
      ;;
  esac
}

SERVER_URL="${1:-http://localhost:8000/mcp}"
run_with_optional_datadog scripts/mcp_smoke.py "$SERVER_URL"
