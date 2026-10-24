#!/usr/bin/env bash
# Shared llmn CLI invocation for repository scripts.
#
# The scripts must run against this checkout, not against a global install.
# Source this file, then call:
#
#   llmn_ensure_installed
#   llmn --help
#
# uv is preferred. If uv is unavailable but mise is installed and the repository
# has a mise config, the helper uses `mise exec -- uv` instead.

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "Source this file instead of executing it." >&2
    exit 1
fi

LLMN_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLMN_PROJECT_DIR="$(cd "${LLMN_SCRIPTS_DIR}/.." && pwd)"
LLMN_USE_MISE=0
LLMN_RUNNER_DESC="uv"
LLMN_RUNNER_READY=0

_llmn_uv() {
    if [[ "${LLMN_USE_MISE}" == "1" ]]; then
        (cd "${LLMN_PROJECT_DIR}" && mise exec -- uv "$@")
    else
        uv "$@"
    fi
}

_llmn_detect_runner() {
    if command -v uv >/dev/null 2>&1; then
        LLMN_USE_MISE=0
        LLMN_RUNNER_DESC="uv"
        LLMN_RUNNER_READY=1
        return 0
    fi

    if command -v mise >/dev/null 2>&1 && [[ -f "${LLMN_PROJECT_DIR}/mise.toml" ]]; then
        LLMN_USE_MISE=1
        LLMN_RUNNER_DESC="mise exec -- uv"
        LLMN_RUNNER_READY=1
        return 0
    fi

    echo "Error: uv is required to run llmn from this checkout." >&2
    echo "Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    echo "Or install mise and trust the repository's mise.toml." >&2
    return 1
}

# Ensure the project environment exists and the local llmn entry point works.
llmn_ensure_installed() {
    _llmn_detect_runner || return 1

    if ! _llmn_uv run --project "${LLMN_PROJECT_DIR}" --no-sync llmn --help >/dev/null 2>&1; then
        echo "Syncing the Python environment with ${LLMN_RUNNER_DESC}..." >&2
        _llmn_uv sync --project "${LLMN_PROJECT_DIR}" --locked --extra dev
    fi

    _llmn_uv run --project "${LLMN_PROJECT_DIR}" --no-sync llmn --help >/dev/null
}

# Run the llmn CLI from this checkout.
llmn() {
    if [[ "${LLMN_RUNNER_READY}" != "1" ]]; then
        _llmn_detect_runner || return 1
    fi
    _llmn_uv run --project "${LLMN_PROJECT_DIR}" --no-sync llmn "$@"
}
