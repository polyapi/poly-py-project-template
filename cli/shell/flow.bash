# Source this file once, then run flow setup. A function can activate this shell.
if command -v flow >/dev/null 2>&1; then
    printf '%s\n' 'flow already exists; keeping the existing command.' >&2
    return 0
fi

flow() {
    local flow_root="$PWD" flow_env flow_status
    while [[ ! -f "$flow_root/cli/flow.py" || ! -f "$flow_root/pyproject.toml" ]]; do
        if [[ "$flow_root" == / ]]; then
            printf '%s\n' 'Run flow inside a Python Flow project.' >&2
            return 1
        fi
        flow_root="$(dirname "$flow_root")"
    done
    if ! command -v uv >/dev/null 2>&1; then
        printf '%s\n' 'Install uv and add it to PATH first.' >&2
        return 1
    fi
    if [[ "${1:-}" == setup ]]; then
        (cd "$flow_root" && UV_PROJECT_ENVIRONMENT=.venv uv sync --locked) || return $?
    fi
    if [[ ! -f "$flow_root/.venv/bin/activate" ]]; then
        printf '%s\n' 'Run flow setup first to create .venv.' >&2
        return 1
    fi
    if [[ "${VIRTUAL_ENV:-}" != "$flow_root/.venv" ]]; then
        source "$flow_root/.venv/bin/activate" || return $?
    fi
    if [[ "${1:-}" == setup ]]; then
        shift
        (cd "$flow_root" && UV_PROJECT_ENVIRONMENT=.venv uv run --locked python cli/flow.py setup --shell bash "$@")
        flow_status=$?
        [[ $flow_status == 0 ]] || return "$flow_status"
        flow_env="$(cd "$flow_root" && UV_PROJECT_ENVIRONMENT=.venv uv run --locked python -m cli.setup --print-envpath)" || return $?
        export ENVPATH="$flow_env"
        return 0
    fi
    flow_env="$(cd "$flow_root" && UV_PROJECT_ENVIRONMENT=.venv uv run --locked python -m cli.setup --print-envpath)" || return $?
    export ENVPATH="$flow_env"
    (cd "$flow_root" && UV_PROJECT_ENVIRONMENT=.venv uv run --locked python cli/flow.py "$@")
}
