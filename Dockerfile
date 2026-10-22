# syntax=docker/dockerfile:1
#
# The upslope server, with the built webapp baked in.
#   docker build -t upslope --build-arg VITE_MAPBOX_TOKEN=pk.... .
# The image keeps the repo layout under /app (python/, data/, webapp/dist/) so the
# package finds its configs and the webapp relative to its own source.

# --- webapp: vite build -> /app/webapp/dist
FROM node:24-slim AS webapp
WORKDIR /app/webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY webapp/ ./
# the annotation tool bundles the control points and copies the images into dist/
COPY data/georegistration/annotations.yaml /app/data/georegistration/annotations.yaml
COPY data/georegistration/images /app/data/georegistration/images
# Public Mapbox token; Vite inlines it into the JS bundle. Without it /map shows a warning.
ARG VITE_MAPBOX_TOKEN=""
RUN VITE_MAPBOX_TOKEN="$VITE_MAPBOX_TOKEN" npm run build


# --- python: locked dependencies + the project (editable) in /app/python/.venv
FROM python:3.12-slim AS python
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app/python
# dependencies first, so code changes don't reinstall them
COPY python/pyproject.toml python/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY python/README.md ./
COPY python/upslope ./upslope
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
# readable by the runtime user whatever the permissions on the host checkout
RUN chmod -R a+rX /app/python


# --- runtime
FROM python:3.12-slim
# rasterio's wheel bundles GDAL but links the system expat
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 upslope
WORKDIR /app
COPY --from=python /app/python /app/python
COPY --chmod=644 data/wrf_codes.yaml /app/data/wrf_codes.yaml
COPY --chmod=644 data/georegistration/annotations.yaml /app/data/georegistration/annotations.yaml
COPY --from=webapp /app/webapp/dist /app/webapp/dist
ENV PATH="/app/python/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UPSLOPE_WEBAPP_DIST=/app/webapp/dist
USER upslope
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
CMD ["upslope", "serve"]
