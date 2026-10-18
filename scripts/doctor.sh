#!/usr/bin/env bash
# Checks that every tool WattSwap needs is installed, and prints install hints for missing ones.
set -u

fail=0

# check <name> <command> <min-version-or-empty> <install-hint>
check() {
  local name="$1" cmd="$2" min="$3" hint="$4" out ver
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf '  [MISSING] %-10s -> %s\n' "$name" "$hint"
    fail=1
    return
  fi
  out="$("$cmd" --version 2>&1 | head -n1)"
  ver="$(printf '%s' "$out" | grep -Eo '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1)"
  if [ -n "$min" ] && [ -n "$ver" ]; then
    if [ "$(printf '%s\n%s\n' "$min" "$ver" | sort -V | head -n1)" != "$min" ]; then
      printf '  [OLD]     %-10s %s (need >= %s) -> %s\n' "$name" "$ver" "$min" "$hint"
      fail=1
      return
    fi
  fi
  printf '  [ok]      %-10s %s\n' "$name" "${ver:-$out}"
}

echo "WattSwap doctor"
check node    node    22.0   "Install Node 22+: https://nodejs.org (or: nvm install 22)"
check pnpm    pnpm    9.0    "corepack enable && corepack prepare pnpm@9.15.0 --activate"
check uv      uv      0.5    "Windows: winget install astral-sh.uv | macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh"
check forge   forge   1.0    "curl -L https://foundry.paradigm.xyz | bash && foundryup  (Windows: run in Git Bash)"
check anvil   anvil   1.0    "installed together with forge via foundryup"
check cast    cast    1.0    "installed together with forge via foundryup"
check docker  docker  24.0   "Install Docker Desktop: https://docs.docker.com/get-docker/"
check make    make    4.0    "Windows: winget install ezwinports.make | macOS: xcode-select --install | Debian/Ubuntu: apt install make"
check git     git     2.30   "https://git-scm.com/downloads"
check gh      gh      2.0    "Optional (opens PRs): https://cli.github.com"

# Python 3.12 is provisioned by uv from .python-version; verify it is available.
if command -v uv >/dev/null 2>&1; then
  if uv python find 3.12 >/dev/null 2>&1; then
    printf '  [ok]      %-10s %s\n' "python3.12" "$(uv python find 3.12)"
  else
    printf '  [MISSING] %-10s -> uv python install 3.12\n' "python3.12"
    fail=1
  fi
fi

# Docker daemon must be running for `make up`.
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    printf '  [ok]      %-10s daemon reachable\n' "docker-up"
  else
    printf '  [DOWN]    %-10s -> start Docker Desktop (needed for make up)\n' "docker-up"
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "Some checks failed. Fix the items above and re-run: make doctor"
  exit 1
fi
echo "All good."
