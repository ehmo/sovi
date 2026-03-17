"""Runtime ownership and conflict detection for the scheduler."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sovi.db import sync_conn

logger = logging.getLogger(__name__)

_SCHEDULER_LOCK_NAME = "sovi.device.scheduler.singleton.v1"


@dataclass(frozen=True)
class SchedulerConflict:
    """A conflicting external scheduler-like process on the host."""

    pid: int
    ppid: int
    command: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "command": self.command,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class SchedulerOwner:
    """Identity/provenance for the running scheduler owner."""

    instance_id: str
    pid: int
    hostname: str
    started_at: str
    cwd: str
    project_root: str
    python_executable: str
    scheduler_module_path: str
    wda_client_module_path: str
    git_sha: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "cwd": self.cwd,
            "project_root": self.project_root,
            "python_executable": self.python_executable,
            "scheduler_module_path": self.scheduler_module_path,
            "wda_client_module_path": self.wda_client_module_path,
            "git_sha": self.git_sha,
        }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _scheduler_lock_key() -> int:
    digest = hashlib.sha256(_SCHEDULER_LOCK_NAME.encode("utf-8")).digest()[:8]
    key = int.from_bytes(digest, "big", signed=False)
    if key >= 2**63:
        key -= 2**64
    return key


def _git_sha(project_root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha or None


def build_scheduler_owner() -> SchedulerOwner:
    """Capture runtime provenance for the current scheduler owner."""
    from sovi.device import scheduler as scheduler_module
    from sovi.device import wda_client as wda_client_module

    project_root = _project_root()
    started_at = datetime.now(timezone.utc).isoformat()
    pid = os.getpid()
    hostname = socket.gethostname()
    instance_seed = f"{hostname}:{pid}:{started_at}"
    instance_id = hashlib.sha1(instance_seed.encode("utf-8")).hexdigest()[:12]
    return SchedulerOwner(
        instance_id=instance_id,
        pid=pid,
        hostname=hostname,
        started_at=started_at,
        cwd=os.getcwd(),
        project_root=str(project_root),
        python_executable=sys.executable,
        scheduler_module_path=inspect.getfile(scheduler_module),
        wda_client_module_path=inspect.getfile(wda_client_module),
        git_sha=_git_sha(project_root),
    )


def _scheduler_conflict_kind(command: str) -> str | None:
    cmd = command.strip()
    if not cmd:
        return None
    if "/tmp/run_scheduler.py" in cmd:
        return "legacy_wrapper"
    if "sovi scheduler start" in cmd or "python -m sovi scheduler start" in cmd:
        return "external_cli"
    return None


def find_conflicting_scheduler_processes(*, current_pid: int | None = None) -> list[SchedulerConflict]:
    """Find scheduler-like processes that should not coexist with the dashboard runtime."""
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("Failed to inspect process table for scheduler conflicts")
        return []

    conflicts: list[SchedulerConflict] = []
    for raw_line in result.stdout.splitlines():
        parts = raw_line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if current_pid is not None and pid == current_pid:
            continue
        command = parts[2]
        kind = _scheduler_conflict_kind(command)
        if not kind:
            continue
        conflicts.append(SchedulerConflict(pid=pid, ppid=ppid, command=command, kind=kind))
    return conflicts


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_conflicting_scheduler_processes(
    conflicts: list[SchedulerConflict],
    *,
    grace_seconds: float = 2.0,
) -> list[SchedulerConflict]:
    """Terminate conflicting scheduler wrappers. Returns survivors that resisted cleanup."""
    if not conflicts:
        return []

    for conflict in conflicts:
        try:
            os.kill(conflict.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception:
            logger.warning("Failed to terminate scheduler conflict pid=%s", conflict.pid, exc_info=True)

    deadline = time.time() + max(grace_seconds, 0.0)
    while time.time() < deadline:
        alive = [conflict for conflict in conflicts if _pid_exists(conflict.pid)]
        if not alive:
            return []
        time.sleep(0.1)

    survivors = [conflict for conflict in conflicts if _pid_exists(conflict.pid)]
    for conflict in survivors:
        try:
            os.kill(conflict.pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except Exception:
            logger.warning("Failed to SIGKILL scheduler conflict pid=%s", conflict.pid, exc_info=True)

    return [conflict for conflict in conflicts if _pid_exists(conflict.pid)]


class SchedulerInstanceLock:
    """Postgres advisory lock held for the lifetime of the scheduler process."""

    def __init__(self) -> None:
        self._key = _scheduler_lock_key()
        self._conn: Any | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    def acquire(self) -> bool:
        if self._conn is not None:
            return True

        conn = sync_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (self._key,))
                row = cur.fetchone() or {}
            if not bool(row.get("acquired")):
                conn.close()
                return False
            self._conn = conn
            return True
        except Exception:
            conn.close()
            raise

    def release(self) -> None:
        if self._conn is None:
            return

        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
        except Exception:
            logger.warning("Failed to release scheduler advisory lock", exc_info=True)
        finally:
            try:
                self._conn.close()
            finally:
                self._conn = None
