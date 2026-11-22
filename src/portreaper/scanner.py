"""Cross-platform network port & process scanner built on psutil."""

from __future__ import annotations

import os
import socket
import time
from typing import Iterable

import psutil

from portreaper.models import PortEntry, ProcessNode, ScanSummary

PUBLIC_BIND_ADDRS: frozenset[str] = frozenset({"0.0.0.0", "::", "*", ""})
LOCAL_BIND_ADDRS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# Ordered specifically so framework-level tags match before generic runtime tags.
_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("VITE", ("vite", "vitest")),
    ("NEXT.JS", ("next-server", "next dev", "next start", "/next/", "\\next\\", ".next")),
    ("DOCKER", ("docker", "dockerd", "containerd", "com.docker", "wslrelay", "vpnkit")),
    ("POSTGRES", ("postgres", "postmaster", "pg_ctl")),
    ("REDIS", ("redis-server", "redis", "valkey-server", "valkey")),
    ("NGINX", ("nginx",)),
    ("NODE", ("node", "nodejs", "nodemon", "tsx", "ts-node", "bun", "deno", "npm", "pnpm", "yarn")),
    (
        "PYTHON",
        (
            "python",
            "python3",
            "uvicorn",
            "gunicorn",
            "fastapi",
            "flask",
            "django",
            "celery",
            "streamlit",
            "jupyter",
            "pytest",
            "pipenv",
            "poetry",
        ),
    ),
    ("GO", ("go-build", "air", "dlv", "delve", "golang")),
    ("RUST", ("cargo", "rustc", "target/debug", "target\\debug", "target/release", "target\\release")),
)

_EXACT_NAME_TAGS: dict[str, str] = {
    "node": "NODE",
    "node.exe": "NODE",
    "bun": "NODE",
    "bun.exe": "NODE",
    "deno": "NODE",
    "deno.exe": "NODE",
    "python": "PYTHON",
    "python3": "PYTHON",
    "python.exe": "PYTHON",
    "pythonw.exe": "PYTHON",
    "py.exe": "PYTHON",
    "postgres": "POSTGRES",
    "postgres.exe": "POSTGRES",
    "redis-server": "REDIS",
    "redis-server.exe": "REDIS",
    "nginx": "NGINX",
    "nginx.exe": "NGINX",
    "dockerd": "DOCKER",
    "docker-proxy": "DOCKER",
    "com.docker.backend.exe": "DOCKER",
    "go": "GO",
    "go.exe": "GO",
    "cargo": "RUST",
    "cargo.exe": "RUST",
}


def detect_dev_tag(name: str, cmdline: str) -> str:
    """Classify a process into a developer stack tag or 'SYS'."""
    name_l = (name or "").strip().lower()
    cmd_l = (cmdline or "").strip().lower()
    combined = f"{name_l} {cmd_l}"

    # 1. Check framework/service rules first (e.g. Vite / Next.js before generic Node).
    for tag, keywords in _TAG_RULES:
        for kw in keywords:
            if kw in combined:
                # Avoid false-positive "air" or "go" inside unrelated system paths.
                if tag == "GO" and kw == "air":
                    if not (name_l in {"air", "air.exe"} or "/air " in cmd_l or "\\air.exe" in cmd_l):
                        continue
                return tag

    # 2. Exact executable name fallback.
    if name_l in _EXACT_NAME_TAGS:
        return _EXACT_NAME_TAGS[name_l]

    return "SYS"


def is_exposed_address(addr: str) -> bool:
    """Return True when a socket is bound to a wildcard / public interface."""
    normalized = (addr or "").strip().lower()
    if normalized in PUBLIC_BIND_ADDRS:
        return True
    if normalized in LOCAL_BIND_ADDRS or normalized.startswith("127."):
        return False
    # Non-loopback LAN/WAN IP (e.g. 192.168.x.x or 10.x.x.x) is network-reachable.
    return True


def format_uptime(create_time: float | None, now: float | None = None) -> str:
    """Format process elapsed lifetime into compact btop-style units."""
    if not create_time or create_time <= 0:
        return "—"
    current = now if now is not None else time.time()
    elapsed = max(0, int(current - create_time))
    if elapsed < 60:
        return f"{elapsed}s"
    minutes, seconds = divmod(elapsed, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _normalize_bind_addr(addr: str) -> str:
    """Clean IPv4-mapped IPv6 addresses like ::ffff:127.0.0.1."""
    if not addr:
        return "0.0.0.0"
    if addr.startswith("::ffff:"):
        return addr[7:]
    return addr


def _pick_display_bind_addr(addrs: Iterable[str]) -> str:
    """Choose the clearest single display address from deduplicated dual-stack bindings."""
    unique = sorted(set(addrs))
    if not unique:
        return "0.0.0.0"
    if "0.0.0.0" in unique:
        return "0.0.0.0"
    if "::" in unique:
        return "::"
    if "127.0.0.1" in unique:
        return "127.0.0.1"
    if "::1" in unique:
        return "::1"
    return unique[0]


class PortScanner:
    """Stateful cross-platform port scanner that tracks per-process CPU deltas cleanly."""

    def __init__(self) -> None:
        # Cache Process handles by (pid, create_time) so cpu_percent(None) yields real readings.
        self._proc_cache: dict[tuple[int, float], psutil.Process] = {}

    def _get_cached_process(self, pid: int) -> psutil.Process | None:
        try:
            proc = psutil.Process(pid)
            ctime = proc.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return None

        key = (pid, ctime)
        cached = self._proc_cache.get(key)
        if cached is not None:
            return cached
        # Prime CPU measurement on first sight.
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        self._proc_cache[key] = proc
        return proc

    def _prune_cache(self, active_pids: set[int]) -> None:
        stale_keys = [key for key in self._proc_cache if key[0] not in active_pids]
        for key in stale_keys:
            self._proc_cache.pop(key, None)

    def _collect_raw_connections(self) -> tuple[list[object], int]:
        """Retrieve inet sockets with graceful fallback when global net_connections is denied."""
        access_warnings = 0
        try:
            return list(psutil.net_connections(kind="inet")), access_warnings
        except (psutil.AccessDenied, PermissionError, OSError):
            access_warnings += 1

        # Fallback: scan accessible processes individually (works cleanly for non-admin users).
        collected: list[object] = []
        for proc in psutil.process_iter(["pid"]):
            try:
                conn_fn = getattr(proc, "net_connections", None) or getattr(proc, "connections")
                conns = conn_fn(kind="inet")
                for c in conns:
                    collected.append(_ProcConnProxy(c, proc.pid))
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, PermissionError, OSError):
                access_warnings += 1
        return collected, access_warnings

    def scan(
        self,
        *,
        include_udp: bool = False,
        dev_only: bool = False,
        exposed_only: bool = False,
    ) -> tuple[list[PortEntry], ScanSummary]:
        """Scan active listening sockets and return deduplicated PortEntry items and summary."""
        now = time.time()
        raw_conns, access_warnings = self._collect_raw_connections()

        # Deduplicate dual-stack (IPv4 + IPv6) listeners by (port, protocol, pid).
        grouped: dict[tuple[int, str, int | None], set[str]] = {}

        for conn in raw_conns:
            laddr = getattr(conn, "laddr", None)
            if not laddr:
                continue
            port = getattr(laddr, "port", None)
            if not port or port <= 0:
                continue

            sock_type = getattr(conn, "type", socket.SOCK_STREAM)
            status = getattr(conn, "status", "")

            if sock_type == socket.SOCK_STREAM:
                if status != psutil.CONN_LISTEN:
                    continue
                protocol = "TCP"
            elif sock_type == socket.SOCK_DGRAM:
                if not include_udp:
                    continue
                protocol = "UDP"
            else:
                continue

            pid = getattr(conn, "pid", None)
            if pid == 0:
                pid = 0
            ip = _normalize_bind_addr(getattr(laddr, "ip", ""))
            key = (int(port), protocol, pid)
            grouped.setdefault(key, set()).add(ip)

        # Collapse cases where a restricted IPv6 socket has pid=None on the exact same
        # (port, protocol) as a known PID (common on dual-stack Linux/Windows setups).
        known_port_protos = {(port, proto) for (port, proto, pid) in grouped if pid is not None}
        for (port, proto, pid) in list(grouped.keys()):
            if pid is None and (port, proto) in known_port_protos:
                extra_addrs = grouped.pop((port, proto, None))
                for k in grouped:
                    if k[0] == port and k[1] == proto and k[2] is not None:
                        grouped[k].update(extra_addrs)
                        break

        active_pids: set[int] = set()
        entries: list[PortEntry] = []

        # Cache per-PID metadata during this single scan pass so multi-port processes
        # only query OS process tables once per scan.
        pid_meta: dict[int, dict[str, object]] = {}

        for (port, protocol, pid), addrs in grouped.items():
            if pid is not None:
                active_pids.add(pid)
                if pid not in pid_meta:
                    pid_meta[pid] = self._inspect_pid(pid, now)
                meta = pid_meta[pid]
            else:
                access_warnings += 1
                meta = {
                    "name": "System / Restricted",
                    "cpu_percent": 0.0,
                    "memory_mb": 0.0,
                    "uptime_str": "—",
                    "cmdline": "(Access Denied — run with elevated privileges for details)",
                    "cwd": "—",
                    "username": "—",
                    "children_count": 0,
                    "parent_pid": None,
                    "parent_name": "",
                }

            display_addr = _pick_display_bind_addr(addrs)
            exposed = any(is_exposed_address(a) for a in addrs)
            name = str(meta["name"])
            cmdline = str(meta["cmdline"])
            dev_tag = detect_dev_tag(name, cmdline)

            entries.append(
                PortEntry(
                    port=port,
                    protocol=protocol,
                    bind_addr=display_addr,
                    is_public_exposed=exposed,
                    pid=pid,
                    name=name,
                    dev_tag=dev_tag,
                    cpu_percent=float(meta["cpu_percent"]),
                    memory_mb=float(meta["memory_mb"]),
                    uptime_str=str(meta["uptime_str"]),
                    cmdline=cmdline,
                    cwd=str(meta["cwd"]),
                    username=str(meta["username"]),
                    children_count=int(meta["children_count"]),
                    parent_pid=meta["parent_pid"],  # type: ignore[arg-type]
                    parent_name=str(meta["parent_name"]),
                    all_bind_addrs=sorted(addrs),
                )
            )

        self._prune_cache(active_pids)

        # Sort cleanly: dev processes first or ascending by port (port ascending is clearest in TUI).
        entries.sort(key=lambda e: (e.port, e.protocol, e.pid or 0))

        # Compute summary from the full unfiltered snapshot (so top bar always shows true totals).
        seen_dev_pids: set[int] = set()
        total_dev_ram = 0.0
        for e in entries:
            if e.is_dev and e.pid is not None and e.pid not in seen_dev_pids:
                seen_dev_pids.add(e.pid)
                total_dev_ram += e.memory_mb

        summary = ScanSummary(
            total_ports=len(entries),
            dev_ports=sum(1 for e in entries if e.is_dev),
            exposed_ports=sum(1 for e in entries if e.is_public_exposed),
            total_dev_ram_mb=round(total_dev_ram, 1),
            access_warnings=access_warnings,
        )

        if dev_only:
            entries = [e for e in entries if e.is_dev]
        if exposed_only:
            entries = [e for e in entries if e.is_public_exposed]

        return entries, summary

    def _inspect_pid(self, pid: int, now: float) -> dict[str, object]:
        """Safely gather process metadata, tolerating AccessDenied and vanished PIDs."""
        default: dict[str, object] = {
            "name": f"pid:{pid}",
            "cpu_percent": 0.0,
            "memory_mb": 0.0,
            "uptime_str": "—",
            "cmdline": "—",
            "cwd": "—",
            "username": "—",
            "children_count": 0,
            "parent_pid": None,
            "parent_name": "",
        }
        proc = self._get_cached_process(pid)
        if proc is None:
            if pid == 0:
                default["name"] = "System Idle"
            elif pid == 4 and os.name == "nt":
                default["name"] = "System"
            return default

        try:
            default["name"] = proc.name() or default["name"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        try:
            default["cpu_percent"] = round(float(proc.cpu_percent(interval=None)), 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        try:
            mem_info = proc.memory_info()
            default["memory_mb"] = round(mem_info.rss / (1024 * 1024), 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        try:
            default["uptime_str"] = format_uptime(proc.create_time(), now)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        try:
            parts = proc.cmdline()
            if parts:
                default["cmdline"] = " ".join(parts)
            else:
                default["cmdline"] = str(default["name"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            default["cmdline"] = "(Access Denied)"

        try:
            default["cwd"] = proc.cwd() or "—"
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            default["cwd"] = "(Access Denied)"

        try:
            default["username"] = proc.username() or "—"
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        try:
            default["children_count"] = len(proc.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        try:
            parent = proc.parent()
            if parent is not None:
                default["parent_pid"] = parent.pid
                try:
                    default["parent_name"] = parent.name()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    default["parent_name"] = ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        return default


class _ProcConnProxy:
    """Lightweight adapter attaching a process PID to a per-process connection object."""

    __slots__ = ("laddr", "type", "status", "pid")

    def __init__(self, conn: object, pid: int) -> None:
        self.laddr = getattr(conn, "laddr", None)
        self.type = getattr(conn, "type", socket.SOCK_STREAM)
        self.status = getattr(conn, "status", "")
        self.pid = pid


def scan_ports(
    *,
    include_udp: bool = False,
    dev_only: bool = False,
    exposed_only: bool = False,
) -> tuple[list[PortEntry], ScanSummary]:
    """One-shot convenience wrapper around PortScanner.scan()."""
    return PortScanner().scan(
        include_udp=include_udp,
        dev_only=dev_only,
        exposed_only=exposed_only,
    )


def get_entries_for_port(port: int, *, include_udp: bool = True) -> list[PortEntry]:
    """Return all active PortEntry listeners bound to a specific port."""
    entries, _ = scan_ports(include_udp=include_udp)
    return [entry for entry in entries if entry.port == port]


def build_process_tree(pid: int, max_depth: int = 5) -> ProcessNode | None:
    """Construct a recursive ProcessNode tree rooted at `pid`."""
    try:
        root = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None

    def _walk(proc: psutil.Process, depth: int) -> ProcessNode:
        name = f"pid:{proc.pid}"
        cmdline = "—"
        mem_mb = 0.0
        cpu_pct = 0.0
        status = "unknown"
        try:
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        try:
            parts = proc.cmdline()
            cmdline = " ".join(parts) if parts else name
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            cmdline = "(Access Denied)"
        try:
            mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        try:
            cpu_pct = round(float(proc.cpu_percent(interval=None)), 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        try:
            status = proc.status()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass

        children_nodes: list[ProcessNode] = []
        if depth < max_depth:
            try:
                for child in proc.children(recursive=False):
                    children_nodes.append(_walk(child, depth + 1))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                pass

        return ProcessNode(
            pid=proc.pid,
            name=name,
            cmdline=cmdline,
            memory_mb=mem_mb,
            cpu_percent=cpu_pct,
            status=status,
            children=children_nodes,
        )

    return _walk(root, 0)


def _is_port_bindable(port: int, host: str = "127.0.0.1") -> bool:
    """Probe whether a TCP port can be bound on the target host (IPv4 and IPv6 loopback)."""
    families = [(socket.AF_INET, host)]
    if host in {"127.0.0.1", "localhost", "0.0.0.0"} and socket.has_ipv6:
        families.append((socket.AF_INET6, "::1" if host != "0.0.0.0" else "::"))

    for family, bind_host in families:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if os.name != "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
        except OSError:
            return False
        finally:
            sock.close()
    return True


def find_next_free_port(start_port: int, host: str = "127.0.0.1", max_port: int = 65535) -> int:
    """Find the first available TCP port at or above `start_port`."""
    if start_port < 1 or start_port > max_port:
        raise ValueError(f"start_port must be between 1 and {max_port}, got {start_port}")

    # Snapshot active listening ports first to skip occupied ports without extra syscalls.
    occupied: set[int] = set()
    try:
        entries, _ = scan_ports(include_udp=False)
        occupied = {entry.port for entry in entries}
    except Exception:
        occupied = set()

    for candidate in range(start_port, max_port + 1):
        if candidate in occupied:
            continue
        if _is_port_bindable(candidate, host=host):
            return candidate

    raise RuntimeError(f"No free TCP port found between {start_port} and {max_port}")
