# ADR-001: Phase 0 toolchain decisions

Status: accepted · Date: 2026-10-18

## Context

CLAUDE.md fixes the stack. Phase 0 had to make a few concrete choices the charter leaves open.

## Decisions

1. **Foundry libs are not committed.** `contracts/lib/` is git-ignored and installed by `make contracts-deps`
   (`forge install --no-git`, pinned tags: OpenZeppelin v5.1.0, forge-std v1.17.0). Avoids submodules and a large
   vendored tree; CI runs the same target.
2. **Tailwind CSS v4** (`@tailwindcss/postcss`, CSS-first config; no `tailwind.config.ts`). Initially
   scaffolded on v3.4, then switched to v4 before merge at the maintainer's request.
3. **shadcn/ui is initialised with its CLI** (`shadcn@latest init -d -y -b radix`, style `radix-nova`).
   The CLI generates `components.json`, the Button, `cn()` (re-exported from the `cn` package published by
   shadcn-ui) and Geist font wiring. We then replaced its neutral palette with WattSwap tokens in
   `globals.css` (dark default, solar-green primary, amber accent, plus `success`/`warning`). Note
   `-b` means component base (`radix`), not a color, in the current CLI.
4. **Web/wagmi/RainbowKit/viem/TanStack/Zustand/charts are deferred to Phase 6** (agreed with the maintainer).
5. **Makefile uses bash** (`SHELL := bash`, POSIX commands only) so the same file works in Git Bash on Windows
   and on `ubuntu-latest`. Foundry commands go through `$(FORGE)`, which falls back to the official Foundry
   Docker image when `forge` is not installed.
6. **`meter-sim` joins docker-compose in Phase 2.**
7. **Python 3.12 is pinned via `.python-version` and provisioned by uv**; no system Python change required.
8. **Line endings:** `.gitattributes` forces `eol=lf` so shell scripts and Docker entrypoints never get CRLF.
9. **Linting uses the ESLint CLI directly** (`eslint .`) with flat config + `eslint-config-next`, avoiding the
   deprecated `next lint`.
10. **The web container has a Docker healthcheck** (Node `fetch` against `:3000`).

## Consequences

First `make install` needs network access to fetch Foundry libs. Compose pins Anvil to Foundry v1.8.5.
