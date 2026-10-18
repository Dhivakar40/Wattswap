# Build context is the repository root (it needs packages/shared for the contract ABIs):
#   docker build -f services/api/Dockerfile .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /srv
COPY services/api/pyproject.toml services/api/uv.lock ./
RUN uv sync --frozen --no-dev
COPY services/api/ ./
COPY packages/shared/src/abis/json /srv/shared/src/abis/json
COPY packages/shared/src/addresses.json /srv/shared/src/addresses.json
ENV PATH="/srv/.venv/bin:$PATH" SHARED_DIR=/srv/shared
EXPOSE 8000
# Migrate, then serve. (One API instance applies migrations; fine for the demo stack.)
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]
