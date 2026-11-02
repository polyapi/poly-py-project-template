#!/usr/bin/env bash
set +x
set +v
set -euo pipefail

kickstart_source="${BASH_SOURCE[0]:-}"

bootstrap_public_template() (
  for tool in curl tar; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      printf 'kickstart: required command not found: %s\n' "$tool" >&2
      exit 1
    fi
  done
  if ! { exec 3</dev/tty; } 2>/dev/null; then
    printf 'kickstart: run this command from an interactive terminal.\n' >&2
    exit 1
  fi
  bootstrap_dir=$(mktemp -d "${TMPDIR:-/tmp}/poly-kickstart-download.XXXXXXXX")
  trap 'rm -rf -- "$bootstrap_dir"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  printf 'Downloading the public project template...\n'
  curl --fail --silent --show-error --location --proto '=https' \
    'https://codeload.github.com/polyapi/poly-py-project-template/tar.gz/refs/heads/main' \
    --output "$bootstrap_dir/template.tar.gz"
  mkdir -- "$bootstrap_dir/template"
  tar -xzf "$bootstrap_dir/template.tar.gz" --strip-components=1 \
    -C "$bootstrap_dir/template"
  # The running bootstrap owns Ctrl+C policy: SDK cancellation returns 130,
  # while cancelling its optional profile prompt can finish successfully.
  trap ':' INT
  bash "$bootstrap_dir/template/kickstart/kickstart.sh" <&3
)

main() {
  if ! command -v uv >/dev/null 2>&1; then
    printf 'kickstart: install uv and add it to PATH first.\n' >&2
    exit 1
  fi
  local script_dir=''
  if [[ -n "$kickstart_source" && -f "$kickstart_source" ]]; then
    script_dir="$(cd -- "$(dirname -- "$kickstart_source")" && pwd)"
  fi
  if [[ -z "$script_dir" || ! -f "$script_dir/kickstart.py" || ! -f "$script_dir/../.python-version" ]]; then
    bootstrap_public_template
    return
  fi
  python_version="$(<"$script_dir/../.python-version")"
  exec uv run --no-project --no-config --python "$python_version" "$script_dir/kickstart.py"
}

# Parse the complete script before starting, including when piped from curl.
main "$@"
