# Phase 0 — Monorepo scaffold

Branch: `phase-0-scaffold` · Target tag: `v0.0.0` (after approval; roadmap says `v0.N.0`, so `v0.0.0`)

## Starting state

- Git repo with no commits, `origin` = `https://github.com/Dhivakar40/Wattswap.git`, `main` has no upstream (remote branch not found / `[gone]`).
- Only `CLAUDE.md` exists (untracked).
- Local tools: Node 22.17, pnpm 9.15, Docker 28.3, gh 2.101 present.
  **Missing:** `uv`, `forge`/`anvil`, `make`. **Python is 3.10** (project needs 3.12).

## Scope

In: repo layout per §4, tooling, docker-compose, Makefile, CI, README, architecture doc, ADRs for deviations.
Out: any real contract/business logic (Phase 1+). Scaffolds contain only what is needed to prove the toolchain: one trivial contract test, `/health`, one web page.

## Deliverables

| Area | What |
|---|---|
| Root | `README.md`, `Makefile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `.gitattributes`, `.editorconfig`, `pnpm-workspace.yaml`, root `package.json`, `.github/workflows/ci.yml` |
| `contracts/` | Foundry project (`foundry.toml`, solc 0.8.24, OZ v5 + forge-std via git submodules/`forge install`), a placeholder-free smoke test (`Version.t.sol` against a minimal `Version.sol`, removed in Phase 1) |
| `services/api/` | `pyproject.toml` (uv, py3.12, ruff, mypy strict, pytest-asyncio), `app/main.py` with `/health` and `/ready` (checks DB + Redis), `core/config.py` (pydantic-settings), request-ID + JSON logging, empty package dirs per §4 (`auth, models, schemas, routers, services, pricing, matching, chain, ws, workers`), Alembic initialised (no migrations yet), tests for `/health` and config |
| `services/meter-sim/` | `pyproject.toml` + package stub with a `--help` CLI only (real logic Phase 2); no fake readings |
| `apps/web/` | Next.js 15 App Router, TS strict, Tailwind, shadcn/ui init, ESLint+Prettier, Vitest + RTL, one landing page showing real API health status, Dockerfile |
| `packages/shared/` | TS package with `src/index.ts` exporting chain-config type + empty address map; ABI export script arrives in Phase 1 |
| `docs/` | `architecture.md` (§5 diagram in Mermaid + text), `adr/` with ADR-001 (toolchain notes below), `phases/phase-0.md` (this file, updated at end) |
| Docker | Compose services: `postgres:16`, `redis:7`, `anvil` (foundry image), `api`, `web`; healthchecks + `depends_on: condition: service_healthy`. `meter-sim` is added to compose in Phase 2 (§3 lists it, §8 Phase 0 does not) |
| Makefile | `up down test lint fmt build install clean logs` (+ `test-contracts/api/web`, `lint-*`) |
| CI | GitHub Actions jobs: **contracts** (forge fmt --check, build, test), **api** (uv sync, ruff, mypy, pytest with Postgres+Redis services), **web** (pnpm install, eslint, prettier --check, tsc, vitest, next build), **docker** (compose config + build). Slither job added in Phase 1 once contracts exist |

## Environment decisions needing your attention

1. **`make` is not installed on Windows.** The Makefile is still the canonical interface (CI/Linux/macOS). To run it locally I can install GNU Make via `winget install ezwinports.make` (or `choco`). Needs your OK.
2. **`uv` missing** → install via `winget install astral-sh.uv` (or pip). uv will then fetch Python 3.12 itself, so no system Python change is needed.
3. **Foundry missing** → no native Windows-friendly installer is as smooth as WSL; options: (a) `foundryup` under Git Bash (works on Windows now), (b) use the `ghcr.io/foundry-rs/foundry` Docker image for local forge commands. Plan: try (a), fall back to (b) via a `make` wrapper. CI uses `foundry-rs/foundry-toolchain`.
4. **Docker Desktop must be running** for `make up` verification.
5. **Push/PR:** `main` has no commits on the remote. The PR needs a base branch, so I'd push an initial commit containing only `CLAUDE.md` + `.gitignore` to `main` first (a one-time bootstrap, not a force-push), then push `phase-0-scaffold` and open the PR. Needs your OK.

## Proposed implementation order (small commits)

1. `chore: add gitignore, editorconfig, env example` · `docs: add CLAUDE.md and phase-0 plan`
2. `chore(contracts): scaffold foundry project`
3. `feat(api): scaffold fastapi with health endpoints` (+ tests)
4. `chore(meter-sim): scaffold package`
5. `chore(shared): scaffold shared package`
6. `feat(web): scaffold next.js app`
7. `chore(docker): add compose stack and dockerfiles`
8. `chore: add makefile`
9. `ci: add github actions workflow`
10. `docs: readme, architecture, adr`

## Risks

- Toolchain installs on Windows (uv/forge/make) may need manual steps → mitigated by Docker fallbacks.
- Pinned versions drift (Next 15, wagmi v2, OZ v5): I'll check installed package versions rather than guess, and pin exactly via lockfiles.
- `anvil` container needs `--host 0.0.0.0` and a fixed chain id (31337) with a deterministic mnemonic — dev-only, documented.
- Next.js standalone Docker build on Windows bind mounts can be slow; compose uses a built image, not a bind mount, for `web`.
- Web dependencies from §3 (wagmi, RainbowKit, etc.) are installed in Phase 6, not now, to keep Phase 0 lean. Tell me if you want them up front.

## Acceptance criteria

- `make up` brings postgres, redis, anvil, api, web to healthy; `curl localhost:8000/health` → 200, `localhost:3000` renders.
- `make lint`, `make test`, `make fmt` run clean for contracts, api, meter-sim, web, shared.
- CI workflow passes on the PR (green checks).
- README quickstart works on a fresh clone; `.env.example` documents every variable; no secrets committed.
- `docs/phases/phase-0.md` updated with built / verify / limitations at the end.

## Known limitations (expected)

- No business logic, no migrations, no Slither yet.
- Anvil keys are the public well-known dev keys, for local use only.

---

# Outcome (updated at end of phase)

## What was built

Everything in the Deliverables table, with these differences from the plan:

- Foundry was installed natively via `foundryup` (v1.8.5); the Docker fallback exists in the Makefile but was not needed.
- `make doctor` added (checks tools, versions, Python 3.12, Docker daemon; prints install hints).
- Tailwind 3.4, hand-initialised shadcn/ui, dark-default design tokens and TypeScript strict are in place; wagmi/RainbowKit etc. deferred to Phase 6.
- A bootstrap commit (`CLAUDE.md`, `.gitignore`, `.gitattributes`) was pushed to `main` first, as approved.
- Decisions are recorded in `docs/adr/001-phase-0-toolchain.md`.

## How to verify

```bash
make doctor && make install
make lint && make test
make up            # then: curl localhost:8000/health ; curl localhost:8000/ready ; open http://localhost:3000
make down
```

## Verified locally (Windows 11, Git Bash)

- `make lint`, `make test`, `make fmt`, `make build`, `make clean`, `make env`, `make install`, `make doctor`: exit 0.
- Tests: forge 1, api 6, meter-sim 2, shared 2, web 2, all passing.
- `make up`: all 5 services healthy. `/health` 200, `/ready` 200 (postgres ok, redis ok), web page renders "Online (ok)", Anvil `eth_chainId` = 0x7a69 (31337). `make down` clean.

## Known limitations

- No business logic, no DB models or migrations, no Slither job yet (Phase 1).
- `web` container has no Docker healthcheck; `--wait` treats "running" as ready.
- `next lint` is deprecated in Next 16; migrate to ESLint CLI later.
- `docker` CI job only builds images; it does not run `compose up`.
- Anvil keys are public dev keys.
