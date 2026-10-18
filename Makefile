# WattSwap task runner. POSIX shell only (bash). On Windows run inside Git Bash.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Make user-level tool dirs visible even in minimal shells (winget, foundryup, uv installs).
export PATH := $(HOME)/.foundry/bin:$(HOME)/.local/bin:$(LOCALAPPDATA)/Microsoft/WinGet/Links:$(PATH)

COMPOSE ?= docker compose
PY_PROJECTS := services/api services/meter-sim

# Foundry: use the native binary when installed, otherwise the official Docker image,
# so `make test-contracts` etc. behave identically either way.
FOUNDRY_IMAGE ?= ghcr.io/foundry-rs/foundry:v1.8.5
HAS_FORGE := $(shell command -v forge >/dev/null 2>&1 && echo yes || echo no)
ifeq ($(HAS_FORGE),yes)
  FORGE := forge
else
  FORGE := docker run --rm -v "$(CURDIR)/contracts:/work" -w /work --entrypoint forge $(FOUNDRY_IMAGE)
endif

.PHONY: help doctor env install up down logs ps build test lint fmt fmt-check clean \
        test-contracts test-api test-meter-sim test-web test-shared \
        lint-contracts lint-api lint-meter-sim lint-web lint-shared

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

doctor: ## Check required tools and versions
	@bash scripts/doctor.sh

env: ## Create .env from .env.example if missing
	@test -f .env || { cp .env.example .env && echo "created .env"; }

install: contracts-deps ## Install all dependencies
	pnpm install --frozen-lockfile
	@for p in $(PY_PROJECTS); do (cd $$p && uv sync --frozen); done

.PHONY: contracts-deps
contracts-deps: ## Install Foundry libs (forge-std, OpenZeppelin v5) into contracts/lib
	@test -d contracts/lib/forge-std || (cd contracts && $(FORGE) install --no-git foundry-rs/forge-std@v1.17.0)
	@test -d contracts/lib/openzeppelin-contracts || (cd contracts && $(FORGE) install --no-git OpenZeppelin/openzeppelin-contracts@v5.1.0)

up: env ## Build and start postgres, redis, anvil, api, web
	$(COMPOSE) up -d --build --wait
	@$(COMPOSE) ps

down: ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs: ## Tail stack logs
	$(COMPOSE) logs -f --tail=100

ps: ## Show stack status
	$(COMPOSE) ps

build: contracts-deps ## Build contracts and web
	cd contracts && $(FORGE) build
	pnpm --filter @wattswap/web build

# ---- tests -----------------------------------------------------------------
test: test-contracts test-api test-meter-sim test-shared test-web ## Run all tests
test-contracts: contracts-deps
	cd contracts && $(FORGE) test
test-api:
	cd services/api && uv run pytest -q
test-meter-sim:
	cd services/meter-sim && uv run pytest -q
test-shared:
	pnpm --filter @wattswap/shared test
test-web:
	pnpm --filter @wattswap/web test

# ---- lint / typecheck ------------------------------------------------------
lint: lint-contracts lint-api lint-meter-sim lint-shared lint-web ## Lint + typecheck everything
lint-contracts: contracts-deps
	cd contracts && $(FORGE) fmt --check
lint-api:
	cd services/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app tests alembic
lint-meter-sim:
	cd services/meter-sim && uv run ruff check . && uv run ruff format --check . && uv run mypy meter_sim tests
lint-shared:
	pnpm --filter @wattswap/shared lint && pnpm --filter @wattswap/shared typecheck && pnpm --filter @wattswap/shared fmt:check
lint-web:
	pnpm --filter @wattswap/web lint && pnpm --filter @wattswap/web typecheck && pnpm --filter @wattswap/web fmt:check

# ---- formatting ------------------------------------------------------------
fmt: contracts-deps ## Auto-format everything
	cd contracts && $(FORGE) fmt
	@for p in $(PY_PROJECTS); do (cd $$p && uv run ruff check --fix . && uv run ruff format .); done
	pnpm fmt

fmt-check: lint ## Alias: formatting is verified as part of lint

clean: ## Remove build artifacts (not dependencies)
	rm -rf contracts/out contracts/cache apps/web/.next
	@for p in $(PY_PROJECTS); do rm -rf $$p/.mypy_cache $$p/.ruff_cache $$p/.pytest_cache; done
