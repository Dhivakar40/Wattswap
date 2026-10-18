# ADR-001: Phase 0 toolchain decisions

Status: accepted · Date: 2026-10-18

## Context

CLAUDE.md fixes the stack. Phase 0 had to make a few concrete choices the charter leaves open.

## Decisions

1. **Foundry libs are not committed.** `contracts/lib/` is git-ignored and installed by `make contracts-deps`
   (`forge install --no-git`, pinned tags: OpenZeppelin v5.1.0, forge-std v1.17.0). Avoids submodules and a large
   vendored tree; CI runs the same target.
2. **Tailwind CSS v3.4**, not v4. shadcn/ui's token model (`hsl(var(--x))` CSS variables, `tailwind.config.ts`)
   is stable on v3; revisit when moving to v4.
3. **shadcn/ui is initialised by hand** (`components.json`, `cn()` helper, `Button`) rather than via the
   interactive CLI, so the setup is reproducible and reviewable. Further components are added in Phase 6.
4. **Web/wagmi/RainbowKit/viem/TanStack/Zustand/charts are deferred to Phase 6** (agreed with the maintainer).
5. **Makefile uses bash** (`SHELL := bash`, POSIX commands only) so the same file works in Git Bash on Windows
   and on `ubuntu-latest`. Foundry commands go through `$(FORGE)`, which falls back to the official Foundry
   Docker image when `forge` is not installed.
6. **`meter-sim` joins docker-compose in Phase 2.**
7. **Python 3.12 is pinned via `.python-version` and provisioned by uv**; no system Python change required.
8. **Line endings:** `.gitattributes` forces `eol=lf` so shell scripts and Docker entrypoints never get CRLF.
9. **`next lint`** is used for now; it is deprecated in Next 16 and will migrate to the ESLint CLI then.

## Consequences

First `make install` needs network access to fetch Foundry libs. Compose pins Anvil to Foundry v1.8.5.
