#!/usr/bin/env bash
set +x
set +v
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  printf 'kickstart: install uv and add it to PATH first.\n' >&2
  exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_version="$(<"$script_dir/../.python-version")"
exec uv run --no-project --no-config --python "$python_version" "$script_dir/kickstart.py"
