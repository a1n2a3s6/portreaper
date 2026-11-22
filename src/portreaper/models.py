"""Typed data models for PortReaper."""

from __future__ import annotations

from dataclasses import dataclass, field


DEV_TAGS: frozenset[str] = frozenset(
    {
        "VITE",
        "NEXT.JS",
        "NODE",
        "PYTHON",
        "DOCKER",
        "POSTGRES",
        "REDIS",
        "GO",
        "RUST",
        "NGINX",
    }
)


@dataclass(slots=True)
class PortEntry:
    """Represents a single active listening socket and its owning process."""

    port: int
    protocol: str
    bind_addr: str
    is_public_exposed: bool
    pid: int | None
    name: str
    dev_tag: str
    cpu_percent: float
    memory_mb: float
    uptime_str: str
    cmdline: str
    cwd: str
    username: str
    children_count: int
    parent_pid: int | None = None
    parent_name: str = ""
    all_bind_addrs: list[str] = field(default_factory=list)

    @property
    def is_dev(self) -> bool:
        """Return True if this entry belongs to a recognized developer stack."""
        return self.dev_tag != "SYS"

    @property
    def row_key(self) -> str:
        """Stable unique key for TUI DataTable rows across refreshes."""
        pid_part = self.pid if self.pid is not None else "none"
        return f"{self.protocol}:{self.port}:{pid_part}"

    @property
    def pid_display(self) -> str:
        """Human-readable PID string, handling restricted system sockets."""
        return str(self.pid) if self.pid is not None else "—"


@dataclass(slots=True)
class ScanSummary:
    """Aggregated telemetry derived from a port scan snapshot."""

    total_ports: int
    dev_ports: int
    exposed_ports: int
    total_dev_ram_mb: float
    access_warnings: int = 0


@dataclass(slots=True)
class KillResult:
    """Outcome of a graceful or forced process termination attempt."""

    pid: int | None
    port: int | None
    name: str
    success: bool
    force: bool
    killed_pids: list[int] = field(default_factory=list)
    message: str = ""


@dataclass(slots=True)
class ProcessNode:
    """Node in a process tree for CLI `inspect` rendering."""

    pid: int
    name: str
    cmdline: str
    memory_mb: float
    cpu_percent: float
    status: str
    children: list[ProcessNode] = field(default_factory=list)
