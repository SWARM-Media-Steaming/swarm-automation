# AWS infrastructure (Terraform)

Everything the hosted version needs on AWS, as one Terraform root module.
**Nothing here is applied by CI or by any script in this repository.** A human
runs `terraform apply` with their own credentials.

Why Terraform and not CDK: the repository is Rust, Python and plain JavaScript
with no Node build step, and CDK would add a TypeScript toolchain and a
bootstrap stack for what is a fixed, modest set of resources. Terraform is
declarative, reviewable as plain diffs, and `terraform fmt`/`validate` run in
CI without an AWS account. The module has no third-party modules, only the AWS
and `random` providers.

## What it creates

| Concern | Resources |
| --- | --- |
| Network | VPC, two public subnets (load balancer only), two private subnets (API, jobs, database), one NAT gateway, an S3 gateway endpoint, one security group per tier |
| API | ECS cluster, Fargate service (`<prefix>-api`), task definition, ALB (HTTPS, TLS 1.3 policy, HTTP redirects), health check `/api/v1/health` |
| Jobs | Fargate task definition `<prefix>-worker` (container `worker`, read-only root, uid 1000, ephemeral volumes, no inbound). The API's `EcsFargateJobRunner` starts one per repository job with `RunTask` |
| Database | RDS PostgreSQL 17, private, KMS-encrypted, TLS enforced, Multi-AZ and deletion protection by default |
| Objects | S3 bucket (versioned, KMS, TLS-only, no public access) for checkpoints, logs and per-tenant documents |
| Keys and secrets | one KMS key; Secrets Manager secrets (the database DSN is generated, the rest are filled by the operator) |
| Images | two immutable, scanned ECR repositories (`<prefix>/api`, `<prefix>/worker`) |
| IAM | an execution role for the API and one for jobs, the API task role, a job task role with **no permissions**, and a bucket-scoped storage user |

See `docs/web-architecture.md` ("AWS mapping" and "Threat model") for how each
piece answers a requirement and what is not yet covered.

## Apply (by a human, when ready)

1. Create a GitHub App and an ACM certificate for the host name (see
   `docs/web-architecture.md`, "Local stack" for the App's permissions; use the
   `github_callback_url` and `github_webhook_url` outputs for its URLs).
2. Configure an encrypted remote backend (`versions.tf` shows the flags). State
   contains the generated database password.
3. `cp terraform.tfvars.example terraform.tfvars`, fill it in, then
   `terraform init && terraform plan`.
4. Build and push the two images (see "Building and publishing images" in
   `docs/web-architecture.md`) and set `api_image` / `worker_image`.
5. `terraform apply`. The service starts at `api_desired_count = 0` because the
   secrets are still empty.
6. Fill the secrets (values never go through Terraform):

   ```sh
   aws secretsmanager put-secret-value --secret-id swarm/github-client-secret  --secret-string "$CLIENT_SECRET"
   aws secretsmanager put-secret-value --secret-id swarm/github-webhook-secret --secret-string "$WEBHOOK_SECRET"
   aws secretsmanager put-secret-value --secret-id swarm/github-app-private-key --secret-string file://app-private-key.pem
   aws secretsmanager put-secret-value --secret-id swarm/local-key --secret-string "$(openssl rand -base64 32)"
   # The storage user's access key: mint it, store it, and do not keep a copy.
   aws iam create-access-key --user-name swarm-storage --query AccessKey \
     --output json | jq '{access_key_id: .AccessKeyId, secret_access_key: .SecretAccessKey}' \
     | aws secretsmanager put-secret-value --secret-id swarm/storage-s3-credentials --secret-string file:///dev/stdin
   ```

   (Replace `swarm` with your `name_prefix`.)
7. Set `api_desired_count = 1`, apply again, create the DNS record for
   `alb_dns_name`, and sign in.

## Known limits (also in the docs)

- The API still keeps sessions, provider keys and usage in memory
  (`MemoryStore`); the Postgres `Store` is a later step. Hence one API task and
  `api_desired_count <= 1`. Restarting it signs everyone out.
- The KMS key encrypts data at rest but is not yet a provider-key wrapper in the
  API; the API wraps with `SWARM_WEB_LOCAL_KEY` from Secrets Manager.
- A job holds the storage user's key (the worker's S3 client signs with a key,
  not a role), so a compromised job could read other tenants' objects. A
  per-job STS session policy scoped to `tenants/<id>/` is the planned fix.
- Job egress is open on 443; the domain allowlist in
  `web/worker/egress-allowlist.txt` needs a DNS-aware firewall.
- Fargate job log snapshots from CloudWatch are not wired into the API; the
  worker's own log artifacts in S3 are what the Overview reads.
