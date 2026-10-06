# Worker image for local Docker and ECS Fargate (issue #418).
# Both runners use this image and this entrypoint; they differ only by
# configuration. Build from the repository root:
#   docker build -f web/worker/Dockerfile -t swarm-automation-worker .
FROM python:3.13-slim-bookworm

# Pinned. Bump these together; do not install "latest" at build time.
ARG GH_VERSION=2.79.0
ARG NODE_VERSION=22.14.0
ARG CLAUDE_CODE_VERSION=2.1.285
ARG CODEX_VERSION=0.160.1
ARG GROK_VERSION=1.0.46

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git openssl xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) gharch=amd64; nodearch=x64 ;; arm64) gharch=arm64; nodearch=arm64 ;; *) echo "unsupported $arch" >&2; exit 1 ;; esac \
    && curl -fsSL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${gharch}.tar.gz" \
    && tar -C /tmp -xzf /tmp/gh.tgz \
    && install -m 0755 "/tmp/gh_${GH_VERSION}_linux_${gharch}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_${gharch}" \
    && curl -fsSL -o /tmp/node.tgz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${nodearch}.tar.xz" \
    && tar -C /usr/local --strip-components=1 -xJf /tmp/node.tgz \
    && rm /tmp/node.tgz \
    && npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@xai-official/grok@${GROK_VERSION}" \
    && npm cache clean --force \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin swarm

COPY issue_worker /opt/swarm/issue_worker
COPY web/worker/egress-allowlist.txt /opt/swarm/egress-allowlist.txt

# Run the container with a read-only root and tmpfs on /tmp, /workspace and
# /home/swarm. Nothing here is a shared volume. The entrypoint refuses a
# workspace or HOME that already contains files.
USER 1000:1000
WORKDIR /workspace
ENV HOME=/home/swarm \
    AWS_EC2_METADATA_DISABLED=true \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
ENTRYPOINT ["python3", "-I", "/opt/swarm/issue_worker/job_launch.py"]
