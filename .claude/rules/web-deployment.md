# Web deployment rules

The hosted stack's packaging is `docker-compose.yml` (local), `web/Dockerfile` (API
image), `web/worker/Dockerfile*` (job images) and `web/infra/aws/` (Terraform).
`docs/web-architecture.md` ("Local stack", "AWS mapping", "Threat model") is the
contract; `issue_worker/test_web_deploy.py` pins it.

- **Nothing deploys from this repository.** No workflow builds or pushes an image,
  signs in to AWS or runs `terraform apply`. CI only runs `terraform fmt -check`,
  `init -backend=false` and `validate`, `docker compose config`, and the web,
  worker and frontend suites. Publishing is a documented human step.
- **One name, one spelling.** Every `SWARM_*` name in compose, `.env.example` or
  Terraform must be one `web/src/*.rs`, `storage_factory.py` or `job_launch.py`
  reads, and every setting the API requires must be supplied. Job containers get
  the `SWARM_STORAGE_*` set of `bridge::STORAGE_ENV` (credentials as secrets). When
  a variable is added or renamed in code, update compose, `.env.example`, `ecs.tf`
  and the docs together.
- **No credential in the repository or in Terraform state.** Compose carries only
  the throwaway local `dev`/`swarm` values; GitHub App secrets, the sealing key and
  the storage key pair are Secrets Manager values put by hand. The generated
  database DSN is the one value Terraform writes. `.env`, `*.tfstate` and
  `*.tfvars` stay git- and docker-ignored. Never add `aws_iam_access_key`.
- **Least privilege stays checkable.** No `*` action or `service:*` on an Allow
  statement; `resource "*"` only for `ecr:GetAuthorizationToken`. A job's task role
  has no permissions (one explicit deny) and no managed-policy attachments; the job
  execution role reads no secret; the API task role only runs, tags, describes and
  stops the worker task definition's tasks and passes the two job roles.
- **Do not configure what the build cannot honour.** Do not set
  `SWARM_WEB_KMS_KEY_ID` until a KMS `KeyWrapper` exists; do not raise the API past
  one task or `api_desired_count` past 1. The Postgres `Store`
  (`SWARM_WEB_STORE=postgres`) now holds identity, tenants, sessions, keys, usage
  and job slots, but the SSE replay rings and the scheduler are per process:
  keep the cap until those move too, then update the Terraform validation, its
  test and the docs together.
- **A job starts clean on any runtime.** The worker images ship an empty
  `/home/swarm` and `/workspace` owned by uid 1000 (no `--create-home` skeleton),
  because `job_launch.py` refuses a non-empty one and Fargate volumes copy the
  image's files in. On Fargate `SWARM_WEB_JOB_CPU_MILLIS` is CPU units (1024 = 1
  vCPU).
- **The local stack stays local.** Ports bind to `127.0.0.1`; the Docker socket is
  mounted into the API only. A change that publishes a port more widely or mounts
  the socket elsewhere needs a threat-model update.
- **Behaviour changes need the `minor` label** from a trusted author; the worker
  owns `VERSION`.
