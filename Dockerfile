FROM node:24-alpine AS ui
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS python-build
WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.7
COPY pyproject.toml uv.lock LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
RUN groupadd --gid 10001 climate && useradd --uid 10001 --gid climate --create-home climate
WORKDIR /app
COPY --from=python-build /app/.venv /app/.venv
COPY --from=ui /build/frontend/dist /app/frontend/dist
COPY alembic.ini ./
COPY LICENSE ./
COPY migrations ./migrations
RUN mkdir /app/data && chown climate:climate /app/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
USER climate
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health/ready',timeout=3)"
CMD ["python", "-m", "climate_control.cli", "serve"]
