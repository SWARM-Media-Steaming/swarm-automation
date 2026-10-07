"""Deployment contract for the hosted version (issue #421).

The compose file, the AWS Terraform, the Dockerfiles, CI and the docs are
configuration, and configuration drifts silently: a variable renamed in
``web/src/config.rs`` leaves the stack booting without it. These tests read the
files as text (the worker is standard library only, and no YAML or HCL parser
is available) and pin the facts that have broken before or that are security
claims in ``docs/web-architecture.md``:

* every ``SWARM_*`` name a deployment file sets is one the code reads, and
  every setting the API requires is supplied;
* the job container receives storage credentials under the names
  ``storage_factory`` reads;
* the stack binds to loopback, mounts the Docker socket into the API only, and
  carries no real credential;
* the AWS module has no wildcard action or resource beyond the documented
  few, gives a job's task role no permissions, mints no access key, and does
  not pass a KMS key id the build would refuse;
* CI runs the web suites and validates, but never builds, pushes or applies;
* ``scripts/web_smoke.py`` detects a stack that answers wrongly.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path

import storage_factory

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
TERRAFORM = WEB / "infra" / "aws"
sys.path.insert(0, str(REPO / "scripts"))
import web_smoke  # noqa: E402

NAME = re.compile(r"\b(SWARM_[A-Z0-9_]+)\b")

# Variables only the compose file itself consumes (image and Dockerfile
# selection for the worker image build); the API never reads them.
COMPOSE_ONLY = {"SWARM_WORKER_DOCKERFILE", "SWARM_WORKER_IMAGE"}

# What Config::from_lookup refuses to start without.
REQUIRED_BY_API = {
    "SWARM_WEB_PUBLIC_URL",
    "SWARM_WEB_GITHUB_CLIENT_ID",
    "SWARM_WEB_GITHUB_CLIENT_SECRET",
    "SWARM_WEB_GITHUB_WEBHOOK_SECRET",
    "SWARM_WEB_LOCAL_KEY",
}
# Required once the job runner is on.
REQUIRED_FOR_JOBS = {
    "SWARM_WEB_JOB_RUNNER",
    "SWARM_WEB_WORKER_IMAGE",
    "SWARM_WEB_GITHUB_APP_ID",
    "SWARM_WEB_GITHUB_APP_PRIVATE_KEY",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def names_in(text: str) -> set[str]:
    return set(NAME.findall(text))


def names_the_code_reads() -> set[str]:
    """Every ``SWARM_*`` string literal in the backend and the worker's storage seam."""
    found: set[str] = set()
    for source in (WEB / "src").glob("*.rs"):
        found |= set(re.findall(r'"(SWARM_[A-Z0-9_]+)"', read(source)))
    found |= set(storage_factory.REQUIRED)
    found |= names_in(read(REPO / "issue_worker" / "storage_factory.py"))
    found |= names_in(read(REPO / "issue_worker" / "job_launch.py"))
    return found


def compose_text() -> str:
    return read(REPO / "docker-compose.yml")


def service_block(name: str) -> str:
    """The text of one top-level compose service."""
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", compose_text())
    assert match, f"compose has no service {name}"
    return match.group(1)


class ComposeStackTests(unittest.TestCase):
    def test_every_setting_the_stack_names_is_one_the_code_reads(self):
        known = names_the_code_reads() | COMPOSE_ONLY
        for label, text in (
            ("docker-compose.yml", compose_text()),
            ("web/.env.example", read(WEB / ".env.example")),
            ("web/infra/aws/ecs.tf", read(TERRAFORM / "ecs.tf")),
        ):
            unknown = sorted(names_in(text) - known)
            self.assertEqual([], unknown, f"{label} sets names nothing reads (renamed?): {unknown}")

    def test_the_api_service_supplies_everything_the_api_requires(self):
        api = service_block("api")
        for name in sorted(REQUIRED_BY_API | REQUIRED_FOR_JOBS):
            self.assertRegex(api, rf"\b{name}:", f"compose api service does not set {name}")
        self.assertIn("SWARM_WEB_JOB_RUNNER: docker", api)
        self.assertIn("SWARM_WEB_BRIDGE: python", api)

    def test_the_first_admin_bootstrap_has_one_name_everywhere(self):
        # #441: one spelling in the code, compose, .env.example, Terraform and the docs.
        name = "SWARM_WEB_BOOTSTRAP_ADMINS"
        self.assertIn(name, names_the_code_reads())
        self.assertRegex(service_block("api"), rf"\b{name}: \$\{{{name}:-\}}", "optional, empty by default")
        self.assertIn(name, read(WEB / ".env.example"))
        self.assertTrue(assigns(read(TERRAFORM / "ecs.tf"), name, 'join(",", var.bootstrap_admins)'))
        self.assertRegex(read(TERRAFORM / "variables.tf"), r'variable "bootstrap_admins"[^}]*default\s*=\s*\[\]')
        self.assertIn("bootstrap_admins", read(TERRAFORM / "terraform.tfvars.example"))
        self.assertIn(name, read(REPO / "docs" / "web-architecture.md"))
        # An operator-chosen list of accounts, not a credential: never a secret in the task.
        self.assertNotIn(name, read(TERRAFORM / "secrets.tf"))

    def test_required_values_fail_loudly_instead_of_booting_empty(self):
        api = service_block("api")
        for name in sorted(REQUIRED_BY_API - {"SWARM_WEB_PUBLIC_URL"}) + ["SWARM_WEB_GITHUB_APP_ID", "SWARM_WEB_GITHUB_APP_PRIVATE_KEY"]:
            self.assertRegex(api, rf"{name}: \$\{{{name}:\?", f"{name} must be a required interpolation")

    def test_jobs_get_the_storage_names_the_worker_reads(self):
        api = service_block("api")
        for name in storage_factory.REQUIRED:
            self.assertRegex(api, rf"\b{name}:", f"compose does not set {name}")
        # The names config.rs used to forward (and storage_factory never read).
        self.assertNotIn("S3_ACCESS_KEY:", api)
        self.assertNotIn("S3_SECRET_KEY:", api)
        self.assertIn("SWARM_JOB_STORAGE: hosted", api)

    def test_jobs_join_the_network_that_reaches_storage(self):
        text = compose_text()
        self.assertIn("SWARM_WEB_DOCKER_NETWORK: swarm-stack", text)
        self.assertRegex(text, r"(?m)^    name: swarm-stack$")
        for name in ("postgres", "minio", "api"):
            self.assertIn("networks: [stack]", service_block(name))

    def test_only_loopback_ports_are_published(self):
        for port in re.findall(r'(?m)^\s+- "([^"]*:\d+:\d+)"', compose_text()):
            self.assertTrue(port.startswith("127.0.0.1:"), f"{port} is published beyond loopback")

    def test_the_docker_socket_is_mounted_into_the_api_only(self):
        for name in ("postgres", "minio", "minio-bucket", "worker-image"):
            self.assertNotIn("docker.sock", service_block(name))
        api = service_block("api")
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", api)
        self.assertIn("read_only: true", api)
        self.assertIn('cap_drop: ["ALL"]', api)
        self.assertIn("no-new-privileges", api)

    def test_the_api_waits_for_its_dependencies_and_the_worker_image(self):
        api = service_block("api")
        self.assertIn("postgres:\n        condition: service_healthy", api)
        self.assertIn("worker-image:\n        condition: service_completed_successfully", api)
        self.assertIn("minio-bucket:\n        condition: service_completed_successfully", api)

    def test_the_platform_schema_is_applied_to_a_fresh_database(self):
        self.assertIn("./web/migrations:/docker-entrypoint-initdb.d:ro", service_block("postgres"))
        self.assertTrue(list((WEB / "migrations").glob("*.sql")))

    def test_no_real_credential_is_committed(self):
        for path in (REPO / "docker-compose.yml", WEB / ".env.example", WEB / "Dockerfile"):
            text = read(path)
            self.assertNotRegex(text, r"-----BEGIN [A-Z ]*PRIVATE KEY-----", path.name)
            self.assertNotRegex(text, r"\b(?:ghp|ghs|gho|github_pat)_[A-Za-z0-9_]{20,}", path.name)
            self.assertNotRegex(text, r"\bsk-[A-Za-z0-9_-]{20,}", path.name)
            self.assertNotRegex(text, r"AKIA[0-9A-Z]{16}", path.name)
        # The only literal credentials are the throwaway local ones.
        example = read(WEB / ".env.example")
        for line in example.splitlines():
            if re.match(r"SWARM_WEB_[A-Z_]*(SECRET|KEY|TOKEN)=.+", line):
                self.fail(f".env.example ships a value: {line.split('=')[0]}")

    def test_env_files_are_ignored_by_git_and_docker(self):
        gitignore = read(REPO / ".gitignore")
        dockerignore = read(REPO / ".dockerignore")
        self.assertRegex(gitignore, r"(?m)^\.env$")
        self.assertIn("!web/.env.example", gitignore)
        self.assertRegex(dockerignore, r"(?m)^\.env$")
        self.assertRegex(dockerignore, r"(?m)^web/infra/\*\*/\*\.tfstate\*$")

    @unittest.skipUnless(shutil.which("docker"), "docker CLI not installed")
    def test_compose_validates_with_the_example_values_filled_in(self):
        import tempfile

        key = "AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM="
        env = (
            "SWARM_WEB_GITHUB_CLIENT_ID=Iv1.x\nSWARM_WEB_GITHUB_CLIENT_SECRET=x\n"
            "SWARM_WEB_GITHUB_WEBHOOK_SECRET=x\nSWARM_WEB_GITHUB_APP_ID=1\n"
            "SWARM_WEB_GITHUB_APP_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----\n"
            f"SWARM_WEB_LOCAL_KEY={key}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "test.env"
            env_file.write_text(env, encoding="utf-8")
            ok = subprocess.run(
                ["docker", "compose", "--env-file", str(env_file), "-f", str(REPO / "docker-compose.yml"), "config", "-q"],
                capture_output=True, text=True, cwd=directory,
            )
            if "unknown shorthand flag" in ok.stderr or "is not a docker command" in ok.stderr:
                self.skipTest("docker compose plugin not installed")
            self.assertEqual(0, ok.returncode, ok.stderr)
            missing = subprocess.run(
                ["docker", "compose", "--env-file", "/dev/null", "-f", str(REPO / "docker-compose.yml"), "config", "-q"],
                capture_output=True, text=True, cwd=directory,
            )
            self.assertNotEqual(0, missing.returncode, "an empty .env must be refused, not booted")
            self.assertIn("SWARM_WEB_", missing.stderr)


class ImageTests(unittest.TestCase):
    def test_the_api_image_is_non_root_and_pins_what_it_installs(self):
        text = read(WEB / "Dockerfile")
        self.assertRegex(text, r"(?m)^USER 10001:10001$")
        self.assertRegex(text, r"ARG PSYCOPG_VERSION=\d+\.\d+\.\d+")
        self.assertRegex(text, r"FROM docker:\d+\.\d+\.\d+-cli")
        self.assertIn("ENTRYPOINT [\"swarm-web\"]", text)
        self.assertIn("HEALTHCHECK", text)
        self.assertIn("/api/v1/health", text)
        self.assertIn("SWARM_WEB_BRIDGE=python", text)

    def test_the_api_image_ships_only_what_it_serves_and_runs(self):
        text = read(WEB / "Dockerfile")
        for copied in ("issue_worker /opt/swarm/issue_worker", "ui /opt/swarm/ui"):
            self.assertIn(f"COPY {copied}", text)
        self.assertNotIn("COPY . ", text)

    def test_both_worker_images_name_the_same_postgres_driver_and_pin_it(self):
        pins = set()
        for name in ("Dockerfile", "Dockerfile.fixture"):
            text = read(WEB / "worker" / name)
            self.assertIn("SWARM_STORAGE_POSTGRES_DRIVER=psycopg:connect", text, name)
            match = re.search(r"ARG PSYCOPG_VERSION=(\S+)", text)
            self.assertTrue(match, f"{name} does not pin psycopg")
            pins.add(match.group(1))
        pins.add(re.search(r"ARG PSYCOPG_VERSION=(\S+)", read(WEB / "Dockerfile")).group(1))
        self.assertEqual(1, len(pins), f"the images pin different drivers: {pins}")

    def test_a_job_starts_with_an_empty_home_and_workspace_on_any_runtime(self):
        # job_launch refuses a non-empty HOME or workspace. A tmpfs hides the
        # image's files (Docker); a Fargate volume copies them in, so the image
        # must not ship any (no skeleton files from --create-home).
        for name in ("Dockerfile", "Dockerfile.fixture"):
            text = read(WEB / "worker" / name)
            self.assertIn("--no-create-home", text, name)
            self.assertIn("install -d -o 1000 -g 1000 /home/swarm /workspace", text, name)
            self.assertNotIn("--create-home", text, name)

    def test_the_build_context_excludes_state_and_secrets(self):
        ignore = read(REPO / ".dockerignore")
        for entry in (".git", "target", "web/target", "node_modules", ".env"):
            self.assertRegex(ignore, rf"(?m)^{re.escape(entry)}$", entry)


def hcl_blocks(text: str, kind: str):
    """``(label, body)`` of each ``resource "<kind>" "<label>" { ... }`` (brace matched)."""
    for match in re.finditer(rf'resource "{re.escape(kind)}" "([^"]+)" \{{', text):
        depth, index = 1, match.end()
        while depth and index < len(text):
            depth += {"{": 1, "}": -1}.get(text[index], 0)
            index += 1
        yield match.group(1), text[match.end():index - 1]


def assigns(text: str, key: str, value: str, count: int | None = None) -> bool:
    """``key = value`` regardless of how ``terraform fmt`` aligned the equals sign."""
    found = re.findall(rf"(?m)^\s*{re.escape(key)}\s*=\s*{re.escape(value)}\s*$", text)
    return len(found) == count if count is not None else bool(found)


def terraform_text() -> str:
    return "\n".join(read(path) for path in sorted(TERRAFORM.glob("*.tf")))


class AwsInfrastructureTests(unittest.TestCase):
    def test_the_module_declares_the_pieces_the_issue_names(self):
        text = terraform_text()
        for resource in (
            "aws_ecs_cluster", "aws_ecs_service", "aws_ecs_task_definition", "aws_db_instance",
            "aws_s3_bucket", "aws_kms_key", "aws_secretsmanager_secret", "aws_lb", "aws_lb_listener",
            "aws_iam_role", "aws_ecr_repository", "aws_vpc",
        ):
            self.assertIn(f'resource "{resource}"', text, resource)
        self.assertRegex(text, r'resource "aws_ecs_task_definition" "api"')
        self.assertRegex(text, r'resource "aws_ecs_task_definition" "job"')

    def test_the_api_addresses_the_job_definition_by_the_names_the_runner_uses(self):
        text = terraform_text()
        self.assertTrue(assigns(read(TERRAFORM / "ecs.tf"), "SWARM_WEB_ECS_TASK_DEFINITION", "local.job_family"))
        job = dict(hcl_blocks(read(TERRAFORM / "ecs.tf"), "aws_ecs_task_definition"))["job"]
        self.assertTrue(assigns(job, "name", '"worker"'))  # EcsFargateJobRunner overrides "worker"
        self.assertTrue(assigns(job, "family", "local.job_family"))
        self.assertIn("SWARM_WEB_JOB_RUNNER", text)
        self.assertIn('"fargate"', text)

    def test_cpu_is_passed_as_fargate_units_not_millicores(self):
        text = read(TERRAFORM / "ecs.tf")
        self.assertRegex(text, r"SWARM_WEB_JOB_CPU_MILLIS\s*=\s*tostring\(var\.job_cpu\)")
        self.assertRegex(read(TERRAFORM / "variables.tf"), r'variable "job_cpu"[^}]*default\s*=\s*1024')

    def test_a_job_has_no_aws_identity_and_the_api_no_standing_access_key(self):
        text = terraform_text()
        self.assertNotIn('resource "aws_iam_access_key"', text)
        self.assertNotIn("aws_iam_role_policy_attachment", text)
        self.assertNotIn("aws_iam_user_policy_attachment", text)
        role_policies = dict(hcl_blocks(read(TERRAFORM / "iam.tf"), "aws_iam_role_policy"))
        self.assertEqual({"api_execution", "job_execution", "job_task_deny", "api_task"}, set(role_policies))
        job_deny = read(TERRAFORM / "iam.tf").split('data "aws_iam_policy_document" "job_task_deny"')[1].split("}\n}\n")[0]
        self.assertTrue(assigns(job_deny, "effect", '"Deny"'))
        self.assertNotIn('"Allow"', job_deny)

    def test_job_execution_cannot_read_secrets(self):
        iam = read(TERRAFORM / "iam.tf")
        job_exec = iam.split('data "aws_iam_policy_document" "job_execution"')[1].split('resource "aws_iam_role_policy" "job_execution"')[0]
        self.assertNotIn("secretsmanager", job_exec)
        self.assertNotIn("kms:", job_exec)

    def test_wildcards_are_limited_to_what_aws_cannot_name_ahead_of_time(self):
        iam = read(TERRAFORM / "iam.tf")
        # No Allow statement grants "*" or "service:*".
        for statement in re.findall(r"statement \{(.*?)\n  \}", iam, flags=re.S):
            if assigns(statement, "effect", '"Deny"'):
                continue
            actions = re.search(r"actions\s*=\s*\[(.*?)\]", statement, flags=re.S)
            self.assertTrue(actions, statement[:80])
            for action in re.findall(r'"([^"]+)"', actions.group(1)):
                self.assertNotRegex(action, r"^\*$|:\*$", f"wildcard action {action}")
            if re.search(r'resources\s*=\s*\["\*"\]', statement):
                self.assertIn("ecr:GetAuthorizationToken", statement, "only the ECR token may use resource *")
        # The one deliberately broad Deny is the job task role's.
        self.assertTrue(assigns(iam, "effect", '"Deny"', count=1))

    def test_the_api_may_start_only_the_worker_definition_and_pass_only_job_roles(self):
        iam = read(TERRAFORM / "iam.tf")
        api_task = iam.split('data "aws_iam_policy_document" "api_task"')[1].split('resource "aws_iam_role_policy" "api_task"')[0]
        self.assertIn("task-definition/${local.job_family}:*", api_task)
        self.assertIn('"ecs:cluster"', api_task)
        self.assertIn("iam:PassedToService", api_task)
        self.assertIn("[aws_iam_role.job_execution.arn, aws_iam_role.job_task.arn]", api_task)
        for action in ("ecs:RunTask", "ecs:TagResource", "ecs:DescribeTasks", "ecs:StopTask"):
            self.assertIn(action, api_task)
        self.assertNotRegex(api_task, r"s3:|secretsmanager:|kms:")

    def test_secrets_never_enter_terraform_state_except_the_generated_dsn(self):
        text = terraform_text()
        self.assertEqual(1, text.count("secret_string"), "only the generated DSN is written by Terraform")
        self.assertNotRegex(text, r"variable \"[a-z_]*(secret|password|private_key|token)[a-z_]*\"")
        self.assertIn("sslmode=require", text)

    def test_the_api_refuses_what_it_cannot_honour(self):
        # config.rs refuses SWARM_WEB_KMS_KEY_ID without a wrapper: do not set it.
        self.assertNotIn("SWARM_WEB_KMS_KEY_ID", terraform_text())
        self.assertNotIn("SWARM_WEB_KMS_KEY_ID", compose_text())
        self.assertIn("SWARM_WEB_LOCAL_KEY", read(TERRAFORM / "ecs.tf"))

    def test_every_required_setting_reaches_the_api_task(self):
        ecs = read(TERRAFORM / "ecs.tf")
        for name in sorted(REQUIRED_BY_API | REQUIRED_FOR_JOBS):
            self.assertIn(name, ecs, f"ecs.tf does not supply {name}")
        for name in storage_factory.REQUIRED:
            self.assertIn(name, ecs, f"ecs.tf does not supply {name}")
        self.assertIn("SWARM_WEB_ECS_SUBNETS", ecs)

    def test_the_deployments_select_the_postgres_store(self):
        # config.rs: SWARM_WEB_STORE=postgres reads SWARM_STORAGE_POSTGRES_DSN.
        self.assertIn("SWARM_WEB_STORE: postgres", service_block("api"))
        self.assertTrue(assigns(read(TERRAFORM / "ecs.tf"), "SWARM_WEB_STORE", '"postgres"'))
        self.assertIn("SWARM_STORAGE_POSTGRES_DSN", service_block("api"))
        self.assertIn("SWARM_STORAGE_POSTGRES_DSN", read(TERRAFORM / "ecs.tf"))
        self.assertIn("SWARM_WEB_STORE", read(WEB / "src" / "config.rs"))

    def test_one_api_task_while_runtime_state_is_per_process(self):
        variables = read(TERRAFORM / "variables.tf")
        self.assertRegex(variables, r'variable "api_desired_count"[^}]*default\s*=\s*0')
        self.assertIn("<= 1", variables)
        self.assertTrue(assigns(read(TERRAFORM / "ecs.tf"), "deployment_minimum_healthy_percent", "0"))

    def test_data_stores_are_private_encrypted_and_tls_only(self):
        rds = dict(hcl_blocks(read(TERRAFORM / "rds.tf"), "aws_db_instance"))["main"]
        self.assertTrue(assigns(rds, "publicly_accessible", "false"))
        self.assertTrue(assigns(rds, "storage_encrypted", "true"))
        self.assertIn('"rds.force_ssl"', read(TERRAFORM / "rds.tf"))
        s3 = read(TERRAFORM / "s3.tf")
        for key in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"):
            self.assertTrue(assigns(s3, key, "true"), key)
        for needle in ("aws:SecureTransport", "aws:kms"):
            self.assertIn(needle, s3)
        self.assertTrue(assigns(s3, "status", '"Enabled"'))  # versioning: checkpoints are overwritten

    def test_only_the_load_balancer_is_public_and_it_is_https(self):
        network = read(TERRAFORM / "network.tf")
        self.assertTrue(assigns(network, "map_public_ip_on_launch", "false", count=1), "public subnets must not auto-assign")
        ecs = read(TERRAFORM / "ecs.tf")
        self.assertTrue(assigns(ecs, "assign_public_ip", "false"))
        alb = read(TERRAFORM / "alb.tf")
        self.assertTrue(assigns(alb, "protocol", '"HTTPS"'))
        self.assertIn("TLS13", alb)
        self.assertTrue(assigns(alb, "status_code", '"HTTP_301"'))
        self.assertTrue(assigns(alb, "path", '"/api/v1/health"'))
        # The job security group has no ingress rule at all.
        self.assertNotRegex(network, r'aws_vpc_security_group_ingress_rule" "job')

    def test_container_hardening_matches_the_docker_runner(self):
        ecs = read(TERRAFORM / "ecs.tf")
        self.assertTrue(assigns(ecs, "readonlyRootFilesystem", "true", count=2))
        self.assertTrue(assigns(ecs, "user", '"1000:1000"', count=1))
        self.assertTrue(assigns(ecs, "user", '"10001:10001"', count=1))
        self.assertEqual(2, ecs.count('capabilities = { drop = ["ALL"] }'))
        self.assertIn('{ name = "AWS_EC2_METADATA_DISABLED", value = "true" }', ecs)

    def test_terraform_is_never_applied_by_anything_in_the_repository(self):
        for path in (REPO / ".github" / "workflows").glob("*.yml"):
            text = read(path)
            self.assertNotRegex(text, r"terraform\s+(apply|destroy)", path.name)
        self.assertTrue((TERRAFORM / ".terraform.lock.hcl").exists(), "commit the provider lock file")
        self.assertNotIn(".tfstate", "\n".join(p.name for p in TERRAFORM.iterdir()))

    @unittest.skipUnless(shutil.which("terraform"), "terraform not installed")
    def test_terraform_files_are_formatted(self):
        result = subprocess.run(
            ["terraform", f"-chdir={TERRAFORM}", "fmt", "-check", "-recursive", "-diff"],
            capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class CiAndDocsTests(unittest.TestCase):
    def test_ci_runs_the_web_suites_and_validates_the_deployment_files(self):
        ci = read(REPO / ".github" / "workflows" / "ci.yml")
        self.assertRegex(ci, r"working-directory: web\n\s+run: \|\n(?:.*\n)*?\s+cargo test --locked")
        self.assertIn("python3 -m unittest discover -p 'test_*.py'", ci)
        self.assertIn("npm test", ci)
        self.assertIn("terraform fmt -check -recursive", ci)
        self.assertIn("terraform validate", ci)
        self.assertIn("init -backend=false", ci)
        self.assertIn("docker compose", ci)
        self.assertIn("web_smoke", ci)

    def test_ci_does_not_build_publish_or_deploy_images(self):
        for path in (REPO / ".github" / "workflows").glob("*.yml"):
            text = read(path)
            for forbidden in ("docker push", "docker/login-action", "docker/build-push-action", "aws-actions/configure-aws-credentials", "ecr get-login-password", "push: true"):
                self.assertNotIn(forbidden, text, f"{path.name}: {forbidden}")

    def test_the_architecture_document_covers_every_part_of_the_contract(self):
        doc = read(REPO / "docs" / "web-architecture.md")
        for heading in (
            "## Local stack", "## API contract", "## Server-Sent Events", "## Tenancy model", "## Runner abstraction",
            "## AWS mapping", "## Threat model", "## Decisions", "### Acceptance flow", "### Building and publishing images",
        ):
            self.assertRegex(doc, rf"(?m)^{re.escape(heading)}\b", f"missing {heading}")
        self.assertNotIn("### Not yet built", doc, "replace the stale list with 'Known gaps'")
        self.assertIn("## Known gaps", doc)

    def test_the_readme_quick_start_is_a_working_sequence(self):
        readme = read(REPO / "README.md")
        section = readme.split("## Hosted web version", 1)[1].split("\n## ", 1)[0]
        for step in ("cp web/.env.example .env", "docker compose up --build", "http://localhost:8080", "scripts/web_smoke.py"):
            self.assertIn(step, section)
        self.assertLess(section.index("cp web/.env.example .env"), section.index("docker compose up --build"))

    def test_agents_and_the_skill_point_at_the_web_path(self):
        for path in (REPO / "AGENTS.md", REPO / ".claude" / "skills" / "swarm-automation-dev" / "SKILL.md"):
            text = read(path)
            for needle in ("docker-compose.yml", "web/infra/aws", "test_web_deploy"):
                self.assertIn(needle, text, f"{path.name} does not mention {needle}")
        self.assertTrue((REPO / ".claude" / "rules" / "web-deployment.md").exists())


class _Stack(http.server.BaseHTTPRequestHandler):
    """A stand-in for a stack. ``mode`` picks which promise it breaks."""

    mode = "good"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"{}", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        mode = type(self).mode
        csp = {"Content-Security-Policy": "default-src 'none'; script-src 'self'", "X-Content-Type-Options": "nosniff"}
        path = self.path
        if path == "/api/v1/health":
            return self._send(200 if mode != "down" else 503, b'{"status":"ok"}')
        if path == "/":
            weak = {"Content-Security-Policy": "default-src 'self' 'unsafe-inline'"}
            return self._send(200, b"<html></html>", weak if mode == "weak-csp" else csp)
        if path == "/api/v1/session":
            body = {"authenticated": False}
            if mode == "leaky-session":
                body["csrf_token"] = "abc"
            return self._send(200, json.dumps(body).encode())
        if path == "/api/v1/tenants":
            return self._send(200 if mode == "open-tenants" else 401)
        if path.startswith("/api/v1/tenants/"):
            return self._send(200 if mode == "open-tenants" else 401)
        if path == "/api/v1/webhooks/github":
            return self._send(200 if mode == "open-webhook" else 401)
        if path.startswith("/api/"):
            return self._send(404, b'{"error":"not found"}')
        if path in ("/.env",) and mode == "serves-dotfiles":
            return self._send(200, b"SECRET=1")
        return self._send(404, b"nope")

    do_GET = do_POST = do_PUT = _handle


class SmokeScriptTests(unittest.TestCase):
    def run_against(self, mode):
        _Stack.mode = mode
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stack)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return {r.name: r for r in web_smoke.checks(f"http://127.0.0.1:{server.server_port}")}
        finally:
            server.shutdown()
            server.server_close()

    def test_a_correct_stack_passes_every_check(self):
        results = self.run_against("good")
        self.assertEqual(8, len(results))
        self.assertTrue(all(r.ok for r in results.values()), [r for r in results.values() if not r.ok])

    def test_each_broken_promise_is_reported_by_name(self):
        expectations = {
            "down": "health",
            "weak-csp": "ui served with a strict CSP",
            "leaky-session": "anonymous session is empty",
            "open-tenants": "tenants need a session",
            "open-webhook": "unsigned webhook refused",
            "serves-dotfiles": "dotfiles and tests are not served",
        }
        for mode, failing in expectations.items():
            results = self.run_against(mode)
            self.assertFalse(results[failing].ok, f"{mode} should fail {failing!r}")

    def test_an_unreachable_stack_fails_every_check_without_a_traceback(self):
        results = web_smoke.checks("http://127.0.0.1:9")
        self.assertTrue(results and not any(r.ok for r in results))
        self.assertTrue(all("unreachable" in r.detail for r in results))
        self.assertEqual(1, web_smoke.main(["web_smoke.py", "http://127.0.0.1:9"]))


if __name__ == "__main__":
    unittest.main()
