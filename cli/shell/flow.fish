# Source this file once, then run flow setup.
if type -q flow
    echo 'flow already exists; keeping the existing command.' >&2
    return 0
end

function flow --description 'Project-local Python Flow'
    set -l flow_root $PWD
    while not test -f "$flow_root/cli/flow.py"; or not test -f "$flow_root/pyproject.toml"
        if test "$flow_root" = /
            echo 'Run flow inside a Python Flow project.' >&2
            return 1
        end
        set flow_root (path dirname "$flow_root")
    end
    if not command -q uv
        echo 'Install uv and add it to PATH first.' >&2
        return 1
    end
    if test "$argv[1]" = setup
        pushd "$flow_root"; or return 1
        env UV_PROJECT_ENVIRONMENT=.venv uv sync --locked
        set -l flow_status $status
        popd
        test $flow_status -eq 0; or return $flow_status
    end
    if not test -f "$flow_root/.venv/bin/activate.fish"
        echo 'Run flow setup first to create .venv.' >&2
        return 1
    end
    if test "$VIRTUAL_ENV" != "$flow_root/.venv"
        source "$flow_root/.venv/bin/activate.fish"; or return $status
    end
    pushd "$flow_root"; or return 1
    if test "$argv[1]" = setup
        env UV_PROJECT_ENVIRONMENT=.venv uv run --locked python cli/flow.py setup --shell fish $argv[2..-1]
        set -l flow_status $status
        if test $flow_status -eq 0
            set -l flow_env (env UV_PROJECT_ENVIRONMENT=.venv uv run --locked python -m cli.setup --print-envpath)
            set flow_status $status
            if test $flow_status -eq 0
                set -gx ENVPATH $flow_env
            end
        end
        popd
        return $flow_status
    end
    set -l flow_env (env UV_PROJECT_ENVIRONMENT=.venv uv run --locked python -m cli.setup --print-envpath)
    set -l flow_status $status
    if test $flow_status -eq 0
        set -gx ENVPATH $flow_env
        env UV_PROJECT_ENVIRONMENT=.venv uv run --locked python cli/flow.py $argv
        set flow_status $status
    end
    popd
    return $flow_status
end
