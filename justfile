# Builds and tasks across python/ (uv), webapp/ (vite) and Docker.
# `just` lists recipes.

# .env.local (gitignored; see .env.example) provides the S3 settings for python/
# and VITE_MAPBOX_TOKEN for webapp builds
set dotenv-path := ".env.local"

image := "upslope:local"

default:
    @just --list --unsorted

# --- everything

# Python tests and webapp typecheck
test: py-test web-typecheck

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

# Run the export against the S3 settings in .env.local
[group('python')]
[working-directory('python')]
py-export *args:
    uv run upslope export-web {{ args }}

# Run the server against the S3 settings in .env.local
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

[group('webapp')]
[working-directory('webapp')]
web-build:
    npm run build

# --- docker

[group('docker')]
docker-build:
    docker build -t {{ image }} --build-arg VITE_MAPBOX_TOKEN .

# Build and start the local stack (MinIO + server); the server exports at startup
[group('docker')]
up:
    docker compose up -d --build
    @echo "server: http://localhost:8000/map   minio console: http://localhost:9001"

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

# Deploy to Fly.io by hand (merging to main deploys via GitHub Actions; one machine, so the hourly export runs once)
[group('deploy')]
deploy:
    fly deploy --ha=false --build-arg VITE_MAPBOX_TOKEN="${VITE_MAPBOX_TOKEN:-}"
