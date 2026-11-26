# Builds and tasks across python/ (uv), webapp/ (vite) and Docker.
# `just` lists recipes.

# .env.local (gitignored; see .env.example) provides the S3 settings for python/
# and VITE_MAPBOX_TOKEN for webapp builds
set dotenv-path := ".env.local"

image := "upslope:local"

default:
    @just --list --unsorted

# --- everything

# Python tests, webapp typecheck and style lint
test: py-test web-typecheck web-lint

# Build the webapp and the Docker image
build: web-build docker-build

# --- python (uv)

[group('python')]
[working-directory('python')]
py-sync:
    uv sync

[group('python')]
[working-directory('python')]
py-test *args:
    uv run pytest {{ args }}

# Run one export check against the S3 settings in .env.local
[group('python')]
[working-directory('python')]
py-export *args:
    uv run upslope export-web {{ args }}

# Run the exporter (a check every 15 minutes) against the S3 settings in .env.local
[group('python')]
[working-directory('python')]
py-export-loop *args:
    uv run upslope export-loop {{ args }}

# Archive what each model is missing once (-m <model> for one), against the S3 settings in .env.local
[group('python')]
[working-directory('python')]
py-archive *args:
    uv run upslope archive-run {{ args }}

# Run the archiver (a check every 15 minutes) against the S3 settings in .env.local
[group('python')]
[working-directory('python')]
py-archive-loop *args:
    uv run upslope archive-loop {{ args }}

# Run a one-off storage migration (python/migrations/) against the S3 settings in .env.local
[group('python')]
[working-directory('python')]
py-migrate name *args:
    uv run python -m migrations.{{ name }} {{ args }}

# Run the server against the S3 settings in .env.local (it doesn't export; see py-export-loop)
[group('python')]
[working-directory('python')]
py-serve *args:
    uv run upslope serve {{ args }}

# --- webapp (vite)

[group('webapp')]
[working-directory('webapp')]
web-install:
    npm ci

[group('webapp')]
[working-directory('webapp')]
web-dev:
    npm run dev

[group('webapp')]
[working-directory('webapp')]
web-typecheck:
    npm run typecheck

# Style guardrails: no hard-coded colors or fonts outside the design tokens (webapp/STYLE.md)
[group('webapp')]
[working-directory('webapp')]
web-lint:
    npm run lint
    npm run lint:style

[group('webapp')]
[working-directory('webapp')]
web-build:
    npm run build

# --- docker

[group('docker')]
docker-build:
    docker build -t {{ image }} --label upslope.local=true --build-arg VITE_MAPBOX_TOKEN .

# Build and start the local stack (MinIO, server, exporter); the exporter checks at startup
[group('docker')]
up:
    docker compose up -d --build
    @echo "server: http://localhost:8000/map   minio console: http://localhost:9001"

# Each rebuild leaves an old image behind (`just clean`).
# Start the local stack and rebuild the server (and the exporter, for Python edits) when their source changes
[group('docker')]
watch:
    docker compose watch

# Compose labels what `up` builds with its project, and `docker-build` adds upslope.local.
# Remove the untagged upslope images that rebuilds leave behind (other projects' images are left alone)
[group('docker')]
clean:
    docker image prune -f --filter label=com.docker.compose.project=upslope
    docker image prune -f --filter label=upslope.local=true

[group('docker')]
down:
    docker compose down

[group('docker')]
logs *args:
    docker compose logs -f {{ args }}

# Stop the stack and delete MinIO's data, so the next `up` exports from scratch
[group('docker')]
reset:
    docker compose down --volumes

# --- deploy

# Deploy to Fly.io by hand (merging to main deploys via GitHub Actions; --ha=false keeps one exporter and one archiver running)
[group('deploy')]
deploy:
    fly deploy --ha=false --build-arg VITE_MAPBOX_TOKEN="${VITE_MAPBOX_TOKEN:-}"
