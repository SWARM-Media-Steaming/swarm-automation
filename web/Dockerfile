# The hosted API image (issue #421). Serves /api/v1 and the shared ui/ assets.
# Build from the repository root:
#   docker build -f web/Dockerfile -t swarm-automation-api .
#
# The same image runs under docker compose (Docker job runner, see
# docker-compose.yml) and as the ECS Fargate API service (web/infra/aws). It is
# not the worker image: jobs run web/worker/Dockerfile.

# Floating stable on purpose, like CI (Cargo.lock is what pins the crates).
FROM rust:1-slim-bookworm AS build
WORKDIR /src/web
# Dependencies first so a source-only change reuses the cached layer.
COPY web/Cargo.toml web/Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && echo '' > src/lib.rs \
    && cargo build --release --locked 2>/dev/null || true
COPY web/src ./src
COPY web/migrations ./migrations
RUN touch src/main.rs src/lib.rs && cargo build --release --locked --bin swarm-web

# The Docker CLI is only used by DockerJobRunner. It is copied from the
# official CLI image (pinned) instead of installing a package repository.
FROM docker:27.5.1-cli AS dockercli

FROM python:3.13-slim-bookworm
# The worker bridge and the installation-token minter are Python
# (issue_worker/, standard library) and the minter signs with the openssl CLI.
# psycopg is the Postgres driver SWARM_STORAGE_POSTGRES_DRIVER names; the
# bridge reads hosted history through it. Pinned: bump with the worker image.
ARG PSYCOPG_VERSION=3.2.9
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "psycopg[binary]==${PSYCOPG_VERSION}" \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin swarm

COPY --from=build /src/web/target/release/swarm-web /usr/local/bin/swarm-web
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY issue_worker /opt/swarm/issue_worker
COPY ui /opt/swarm/ui

ENV SWARM_WEB_BIND=0.0.0.0:8080 \
    SWARM_WEB_UI_DIR=/opt/swarm/ui \
    SWARM_WEB_WORKER_DIR=/opt/swarm/issue_worker \
    SWARM_WEB_PYTHON=python3 \
    SWARM_WEB_BRIDGE=python \
    SWARM_STORAGE_POSTGRES_DRIVER=psycopg:connect \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD ["python3", "-I", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["swarm-web"]
