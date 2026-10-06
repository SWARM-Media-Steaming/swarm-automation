"""Copy checkpoints and artifacts between a container and the hosted store.

The worker still reads and writes its state directory as local files (those
writes are interleaved with git operations on the same checkout). A hosted job
runs in a container whose filesystem is destroyed when the container exits, so
the entrypoint copies that directory to and from the tenant's hosted storage
around the worker process. A later container, with an empty directory, sees
the exit-13 epoch yield, the quota pause and the exit-14 hold exactly as a new
process on the desktop does.

The worker itself always uses the ``default`` tenant inside its private state
directory. The hosted copy is stored under the real tenant id, so two tenants
never share a prefix. This module does not change worker behavior.
"""

from __future__ import annotations

from pathlib import Path

from storage import (
    CURRENT,
    DEFAULT_TENANT,
    KEYED_CHECKPOINTS,
    SINGLETON_CHECKPOINTS,
    LocalStorage,
    Storage,
)

# Logs and the completed-issue list, plus the delivery proof the job-runner
# acceptance fixture writes. Nothing else in the state directory is copied, so
# a stray file in the container cannot become tenant state.
ARTIFACTS = (
    "completed-issues",
    "delivery-proof",
    "last-ai-diagnostic.log",
    "last-ai-output.log",
)


def hydrate(durable: Storage, tenant: str, state_dir: Path) -> int:
    """Copy ``tenant``'s hosted checkpoints into a fresh local state directory.

    Returns how many checkpoint documents were restored.
    """
    local = LocalStorage(state_dir)
    restored = 0
    for kind in SINGLETON_CHECKPOINTS:
        value = durable.read_checkpoint(tenant, kind, CURRENT)
        if value is not None:
            local.write_checkpoint(DEFAULT_TENANT, kind, CURRENT, value)
            restored += 1
    for kind in KEYED_CHECKPOINTS:
        for key in durable.list_checkpoints(tenant, kind):
            value = durable.read_checkpoint(tenant, kind, key)
            if value is None:
                continue
            local.write_checkpoint(DEFAULT_TENANT, kind, key, value)
            restored += 1
    for name in ARTIFACTS:
        text = durable.read_artifact(tenant, name)
        if text is not None:
            local.write_artifact(DEFAULT_TENANT, name, text)
    return restored


def publish(durable: Storage, tenant: str, state_dir: Path) -> int:
    """Copy the container's state directory back, including deletions.

    A checkpoint the worker removed locally is removed from the hosted store,
    so a finished issue does not resume. Returns how many documents were written.
    """
    if not state_dir.is_dir():
        return 0
    local = LocalStorage(state_dir)
    written = 0
    for kind in SINGLETON_CHECKPOINTS:
        value = local.read_checkpoint(DEFAULT_TENANT, kind, CURRENT)
        if value is None:
            durable.delete_checkpoint(tenant, kind, CURRENT)
        else:
            durable.write_checkpoint(tenant, kind, CURRENT, value)
            written += 1
    for kind in KEYED_CHECKPOINTS:
        present = set(local.list_checkpoints(DEFAULT_TENANT, kind))
        for key in durable.list_checkpoints(tenant, kind):
            if key not in present:
                durable.delete_checkpoint(tenant, kind, key)
        for key in sorted(present):
            value = local.read_checkpoint(DEFAULT_TENANT, kind, key)
            if value is None:
                continue
            durable.write_checkpoint(tenant, kind, key, value)
            written += 1
    for name in ARTIFACTS:
        text = local.read_artifact(DEFAULT_TENANT, name)
        if text is None:
            durable.delete_artifact(tenant, name)
        else:
            durable.write_artifact(tenant, name, text)
    return written
