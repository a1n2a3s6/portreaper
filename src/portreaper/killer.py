"""Cross-platform process terminator with safety guards and recursive tree cleanup."""

from __future__ import annotations

import os
import platform

import psutil

from portreaper.models import KillResult
from portreaper.scanner import get_entries_for_port

PROTECTED_PIDS: frozenset[int] = frozenset({0, 1, 4})

PROTECTED_PROCESS_NAMES: frozenset[str] = frozenset(
    {
        "system",
        "system idle process",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "systemd",
        "init",
        "launchd",
    }
)


def is_protected_pid(pid: int | None, proc_name: str = "") -> tuple[bool, str]:
    """Validate whether a PID is protected from termination by PortReaper's safety guard."""
    if pid is None:
        return True, "Cannot terminate socket with unknown PID (elevated privileges required)."

    if pid in PROTECTED_PIDS:
        return True, f"Safety guard blocked termination of critical OS PID {pid}."

    if pid == os.getpid():
        return True, f"Safety guard refused to kill PortReaper itself (PID {pid})."

    if proc_name and proc_name.strip().lower() in PROTECTED_PROCESS_NAMES:
        return True, f"Safety guard blocked termination of critical OS process '{proc_name}' (PID {pid})."

    return False, ""


def kill_pid(
    pid: int | None,
    *,
    force: bool = False,
    timeout: float = 3.0,
    port: int | None = None,
) -> KillResult:
    """Terminate a single process (graceful) or its full recursive process tree (force).

    - Graceful (`force=False`): sends SIGTERM (Linux) / TerminateProcess (Windows) via `proc.terminate()`.
    - Force (`force=True`): collects all recursive children (`proc.children(recursive=True)`),
      kills children bottom-up and parent via `kill()` (SIGKILL on Linux / hard terminate on Windows),
      and waits for full cleanup so stuck Node/Vite/Python worker trees actually die.
    """
    if pid is None:
        return KillResult(
            pid=None,
            port=port,
            name="Unknown",
            success=False,
            force=force,
            message="No PID associated with this socket (try running as Administrator/root).",
        )

    blocked, reason = is_protected_pid(pid)
    if blocked:
        return KillResult(
            pid=pid,
            port=port,
            name=f"pid:{pid}",
            success=False,
            force=force,
            message=reason,
        )

    try:
        proc = psutil.Process(pid)
        proc_name = proc.name()
    except psutil.NoSuchProcess:
        return KillResult(
            pid=pid,
            port=port,
            name=f"pid:{pid}",
            success=True,
            force=force,
            message=f"Process {pid} already exited.",
        )
    except (psutil.AccessDenied, PermissionError):
        elevated_hint = "Administrator" if platform.system() == "Windows" else "sudo/root"
        return KillResult(
            pid=pid,
            port=port,
            name=f"pid:{pid}",
            success=False,
            force=force,
            message=f"Access denied for PID {pid}. Re-run as {elevated_hint}.",
        )
    except OSError as exc:
        return KillResult(
            pid=pid,
            port=port,
            name=f"pid:{pid}",
            success=False,
            force=force,
            message=f"OS error inspecting PID {pid}: {exc}",
        )

    blocked, reason = is_protected_pid(pid, proc_name)
    if blocked:
        return KillResult(
            pid=pid,
            port=port,
            name=proc_name,
            success=False,
            force=force,
            message=reason,
        )

    killed_pids: list[int] = []
    denied_pids: list[int] = []

    if not force:
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
            killed_pids.append(pid)
            return KillResult(
                pid=pid,
                port=port,
                name=proc_name,
                success=True,
                force=False,
                killed_pids=killed_pids,
                message=f"Gracefully stopped {proc_name} (PID {pid}).",
            )
        except psutil.TimeoutExpired:
            return KillResult(
                pid=pid,
                port=port,
                name=proc_name,
                success=False,
                force=False,
                message=(
                    f"{proc_name} (PID {pid}) ignored graceful stop after {timeout:.1f}s. "
                    "Use Force Kill (K / --force) to terminate its process tree."
                ),
            )
        except psutil.NoSuchProcess:
            killed_pids.append(pid)
            return KillResult(
                pid=pid,
                port=port,
                name=proc_name,
                success=True,
                force=False,
                killed_pids=killed_pids,
                message=f"Stopped {proc_name} (PID {pid}).",
            )
        except (psutil.AccessDenied, PermissionError):
            elevated_hint = "Administrator" if platform.system() == "Windows" else "sudo/root"
            return KillResult(
                pid=pid,
                port=port,
                name=proc_name,
                success=False,
                force=False,
                message=f"Access denied terminating {proc_name} (PID {pid}). Re-run as {elevated_hint}.",
            )
        except OSError as exc:
            return KillResult(
                pid=pid,
                port=port,
                name=proc_name,
                success=False,
                force=False,
                message=f"Failed to stop {proc_name} (PID {pid}): {exc}",
            )

    # Force Kill: recursive bottom-up tree termination (children first, then parent).
    targets: list[psutil.Process] = []
    try:
        children = proc.children(recursive=True)
        # Reverse so deepest leaf workers are killed before intermediate supervisors.
        targets.extend(reversed(children))
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        pass
    targets.append(proc)

    for target in targets:
        try:
            t_blocked, _ = is_protected_pid(target.pid)
            if t_blocked:
                continue
            target.kill()
            killed_pids.append(target.pid)
        except psutil.NoSuchProcess:
            killed_pids.append(target.pid)
        except (psutil.AccessDenied, PermissionError):
            denied_pids.append(target.pid)
        except OSError:
            denied_pids.append(target.pid)

    _, alive = psutil.wait_procs(targets, timeout=timeout)
    if alive:
        for stubborn in alive:
            try:
                stubborn.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
        _, alive = psutil.wait_procs(alive, timeout=1.0)

    if alive:
        alive_ids = ", ".join(str(p.pid) for p in alive)
        return KillResult(
            pid=pid,
            port=port,
            name=proc_name,
            success=False,
            force=True,
            killed_pids=killed_pids,
            message=f"Force kill incomplete — stubborn PIDs still alive: {alive_ids}.",
        )

    if denied_pids and pid in denied_pids:
        elevated_hint = "Administrator" if platform.system() == "Windows" else "sudo/root"
        return KillResult(
            pid=pid,
            port=port,
            name=proc_name,
            success=False,
            force=True,
            killed_pids=killed_pids,
            message=f"Access denied force-killing {proc_name} (PID {pid}). Re-run as {elevated_hint}.",
        )

    tree_note = f" (+ {len(killed_pids) - 1} child worker(s))" if len(killed_pids) > 1 else ""
    return KillResult(
        pid=pid,
        port=port,
        name=proc_name,
        success=True,
        force=True,
        killed_pids=killed_pids,
        message=f"Force killed {proc_name} (PID {pid}){tree_note}.",
    )


def kill_port(port: int, *, force: bool = False, timeout: float = 3.0) -> list[KillResult]:
    """Locate and terminate all processes listening on `port`."""
    entries = get_entries_for_port(port, include_udp=True)
    if not entries:
        return [
            KillResult(
                pid=None,
                port=port,
                name="—",
                success=False,
                force=force,
                message=f"No active process found listening on port {port}.",
            )
        ]

    results: list[KillResult] = []
    seen_pids: set[int] = set()
    for entry in entries:
        if entry.pid is not None:
            if entry.pid in seen_pids:
                continue
            seen_pids.add(entry.pid)
        results.append(kill_pid(entry.pid, force=force, timeout=timeout, port=port))
    return results
