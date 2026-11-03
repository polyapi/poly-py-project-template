#!/usr/bin/env bash
# Sourced by kickstart.sh. A new project needs only curl, plus tar and the base
# shell tools every supported OS ships: a missing uv is installed for the
# current user with Astral's official installer, without sudo, and uv then
# downloads the project's pinned Python. Git and VS Code's `code` command are
# optional; kickstart explains how to finish without them.

KICKSTART_UV_INSTALLER_URL="${KICKSTART_UV_INSTALLER_URL:-https://astral.sh/uv/install.sh}"
toolchain_installed=false

# Print a usable command's path.
toolchain_command() {
  local resolved
  resolved="$(command -v "$1" 2>/dev/null)" || return 1
  printf '%s' "$resolved"
}

# The directories uv's installer can choose, in the order it chooses them.
kickstart_uv_dirs() {
  [[ -z "${UV_INSTALL_DIR:-}" ]] || printf '%s\n%s\n' "$UV_INSTALL_DIR" "$UV_INSTALL_DIR/bin"
  [[ -z "${XDG_BIN_HOME:-}" ]] || printf '%s\n' "$XDG_BIN_HOME"
  [[ -z "${XDG_DATA_HOME:-}" ]] || printf '%s\n' "$XDG_DATA_HOME/../bin"
  printf '%s\n' "$HOME/.local/bin"
}

ensure_uv() {
  local uv_path installer dir
  if uv_path="$(toolchain_command uv)" && "$uv_path" --version >/dev/null 2>&1; then
    return 0
  fi
  printf 'uv was not found. Installing uv for your user with the official installer...\n'
  installer="$(mktemp "${TMPDIR:-/tmp}/kickstart-uv.XXXXXXXX")" || return 1
  if ! curl --fail --silent --show-error --location --proto '=https' \
    "$KICKSTART_UV_INSTALLER_URL" --output "$installer"; then
    rm -f -- "$installer"
    printf 'kickstart: could not download the uv installer.\n' >&2
    return 1
  fi
  # The installer adds uv's directory to the shell profile named by SHELL, so
  # new terminals find uv.
  if ! sh "$installer"; then
    rm -f -- "$installer"
    printf 'kickstart: uv installation failed. See https://docs.astral.sh/uv/getting-started/installation/\n' >&2
    return 1
  fi
  rm -f -- "$installer"
  while IFS= read -r dir; do
    if [[ -x "$dir/uv" ]]; then
      export PATH="$dir:$PATH"
      break
    fi
  done < <(kickstart_uv_dirs)
  hash -r
  if ! uv --version >/dev/null 2>&1; then
    printf 'kickstart: uv was installed but cannot be found. Open a new terminal and run kickstart again.\n' >&2
    return 1
  fi
  toolchain_installed=true
  KICKSTART_UV_INSTALLED_DIR="$(dirname -- "$(command -v uv)")"
  export KICKSTART_UV_INSTALLED_DIR
}
