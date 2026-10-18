# LABELED SIMULATOR image. Not real hardware.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY meter_sim ./meter_sim
ENV PATH="/srv/.venv/bin:$PATH"
ENTRYPOINT ["python", "-m", "meter_sim"]
CMD ["--help"]
