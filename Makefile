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
        lint-contracts lint-api lint-meter-sim lint-web lint-shared \
        coverage-contracts slither export-abis deploy-local gen-vectors test-contracts-ci \
        migrate sim-provision sim test-integration pricing backtest trading trade-demo

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

# ---- deployment ------------------------------------------------------------
# deploy-local: needs Anvil running (`make up`). Uses Anvil account #0 as admin and the
# role keys from .env.example for relayer/oracle/pricer. Public dev keys only.
# Requires native Foundry (cast); the Docker fallback only covers build/test.
ANVIL_RPC ?= http://127.0.0.1:8545
export-abis: ## Export ABIs + deployed addresses to packages/shared
	pnpm --filter @wattswap/shared exec node scripts/export-abis.mjs
.PHONY: export-abis-only
export-abis-only: contracts-deps ## Rebuild contracts and export ABIs only
	cd contracts && $(FORGE) build
	pnpm --filter @wattswap/shared exec node scripts/export-abis.mjs --abis-only
deploy-local: contracts-deps ## Deploy to local Anvil and export ABIs/addresses
	cd contracts && $(FORGE) build
	cd contracts && PRIVATE_KEY=$$(cast wallet private-key "test test test test test test test test test test test junk" --mnemonic-index 0) 	  RELAYER_ADDRESS=$$(cast wallet address --private-key $$(grep ^RELAYER_PRIVATE_KEY ../.env.example | cut -d= -f2)) 	  MINTER_ADDRESS=$$(cast wallet address --private-key $$(grep ^ORACLE_PRIVATE_KEY ../.env.example | cut -d= -f2)) 	  REGISTRAR_ADDRESS=$$(cast wallet address --private-key $$(grep ^REGISTRAR_PRIVATE_KEY ../.env.example | cut -d= -f2)) 	  PRICER_ADDRESS=$$(cast wallet address --private-key $$(grep ^PRICER_PRIVATE_KEY ../.env.example | cut -d= -f2)) 	  $(FORGE) script script/Deploy.s.sol --rpc-url $(ANVIL_RPC) --broadcast
	$(MAKE) export-abis
# deploy-sepolia: requires PRIVATE_KEY and SEPOLIA_RPC_URL in the environment (see .env.example).
.PHONY: deploy-sepolia
deploy-sepolia: contracts-deps ## Deploy to Sepolia (Phase 8 uses this; needs PRIVATE_KEY + SEPOLIA_RPC_URL)
	cd contracts && $(FORGE) script script/Deploy.s.sol --rpc-url "$$SEPOLIA_RPC_URL" --broadcast --verify
	$(MAKE) export-abis

# ---- backend / simulator ----------------------------------------------------------
migrate: ## Apply database migrations (needs postgres from `make up`)
	cd services/api && uv run alembic upgrade head

# DEV ONLY: the simulator's demo provisioning signs in as an owner wallet. Default: Anvil #5.
OWNER_PRIVATE_KEY ?= 0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba
SIM_DIR := services/meter-sim/runtime
sim-provision: ## DEV ONLY: register a demo (simulated) meter via the API; writes $(SIM_DIR)/meters.json
	@mkdir -p $(SIM_DIR)
	@test -f $(SIM_DIR)/device.key || (cd services/meter-sim && uv run python -m meter_sim keygen --out runtime/device.key)
	cd services/meter-sim && OWNER_PRIVATE_KEY=$(OWNER_PRIVATE_KEY) uv run python -m meter_sim provision 	  --api http://localhost:$${API_PORT:-8000} --device-key runtime/device.key --config runtime/meters.json
$(SIM_DIR)/meters.json:
	@$(MAKE) --no-print-directory sim-provision
sim: $(SIM_DIR)/meters.json ## Run the SIMULATED meter in accelerated DEMO mode (1 interval / 20 s); provisions once

	cd services/meter-sim && uv run python -m meter_sim run --api http://localhost:$${API_PORT:-8000} 	  --config runtime/meters.json --demo --demo-seconds 20

pricing: ## Run the pricing engine as its own process (set PRICING_EMBEDDED=false on the API)
	cd services/api && uv run python -m app.pricing.run
trading: ## Run matcher/relayer/listener/sweeper as their own process (set TRADING_EMBEDDED=false on the API)
	cd services/api && uv run python -m app.trading.run
trade-demo: ## DEMO: two wallets place crossing orders against the running stack and settle on Anvil
	cd services/api && uv run python -m dev.trade_demo
backtest: ## Replay the engine over history: make backtest ARGS="--lat 13.08 --lon 80.27 --from 2026-06-01 --to 2026-06-07"
	cd services/api && uv run python -m app.pricing.backtest $(ARGS)

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
test-contracts-ci: contracts-deps ## Contract tests with the heavy fuzz/invariant profile
	cd contracts && FOUNDRY_PROFILE=ci $(FORGE) test
coverage-contracts: contracts-deps ## Fail if contracts/src line coverage < 100%
	FORGE="$(FORGE)" bash scripts/check-coverage.sh
slither: contracts-deps ## Static analysis (fails on medium/high)
	cd contracts && slither . --config-file slither.config.json --fail-medium
gen-vectors: contracts-deps ## Regenerate packages/shared/vectors/eip712.json from the contracts
	cd contracts && WRITE_VECTORS=1 $(FORGE) test --match-contract VectorsTest
test-api: ## API unit + API tests + the Anvil end-to-end pipeline (needs postgres+redis: `make up`)
	cd services/api && uv run pytest -q
test-integration: ## Only the end-to-end meter -> mint pipeline on Anvil (fails, not skips, if a service is down)
	cd services/api && REQUIRE_SERVICES=1 uv run pytest -q -m integration
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
