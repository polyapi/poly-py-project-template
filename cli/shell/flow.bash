# Source this file once, then run flow setup. A function can activate this shell.
if command -v flow >/dev/null 2>&1; then
    printf '%s\n' 'flow already exists; keeping the existing command.' >&2
    return 0
fi

flow() {
    local flow_root="$PWD" flow_env flow_status flow_python flow_entry
    while [[ ! -f "$flow_root/cli/flow.py" || ! -f "$flow_root/pyproject.toml" ]]; do
        if [[ "$flow_root" == / ]]; then
            printf '%s\n' 'Run flow inside a Python Flow project.' >&2
            return 1
        fi
        flow_root="$(dirname "$flow_root")"
    done
    # Local maintenance must not select a target or install dependencies.
    if [[ "${1:-}" == self-update || "${1:-}" == --version || "${1:-}" == -V ]]; then
        if [[ -x "$flow_root/.venv/bin/python" ]]; then
            flow_python="$flow_root/.venv/bin/python"
        else
            flow_python="$(command -v python3 || command -v python)"
        fi
        if [[ -z "$flow_python" ]] || ! "$flow_python" -B -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
            printf '%s\n' 'Python 3.11 or newer is required for local Flow maintenance.' >&2
            return 1
        fi
        flow_entry=cli/flow.py
        if [[ "${1:-}" == self-update ]]; then
            flow_entry=cli/self_update.py
            shift
        fi
        (cd "$flow_root" && "$flow_python" -B "$flow_entry" "$@")
        return $?
    fi
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
