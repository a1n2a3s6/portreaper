"""Typer CLI entry point for PortReaper."""

from __future__ import annotations

import platform
import sys

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from portreaper import __version__
from portreaper.killer import kill_port
from portreaper.models import ProcessNode
from portreaper.scanner import (
    build_process_tree,
    find_next_free_port,
    get_entries_for_port,
    scan_ports,
)

console = Console()

TAG_COLORS: dict[str, str] = {
    "VITE": "bold #F59E0B",
    "NEXT.JS": "bold #E6EDF3",
    "NODE": "bold #10B981",
    "PYTHON": "bold #38BDF8",
    "DOCKER": "bold #0EA5E9",
    "POSTGRES": "bold #60A5FA",
    "REDIS": "bold #EF4444",
    "GO": "bold #22D3EE",
    "RUST": "bold #FB923C",
    "NGINX": "bold #34D399",
    "SYS": "#8B949E",
}

app = typer.Typer(
    name="portreaper",
    help="PortReaper — Cross-platform interactive Port & Dev Process Sniper for Windows & Linux.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold #E6EDF3]PortReaper[/] [bold #10B981]v{__version__}[/] ({platform.system()})")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def default_entry(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show PortReaper version and exit.",
    ),
    dev: bool = typer.Option(False, "--dev", "-d", help="Start TUI in Dev Ports Only mode."),
    udp: bool = typer.Option(False, "--udp", help="Include UDP bindings in TUI."),
) -> None:
    """Launch the interactive PortReaper TUI when invoked with no subcommand."""
    if ctx.invoked_subcommand is None:
        from portreaper.tui.app import run_tui

        run_tui(dev_only=dev, include_udp=udp)


@app.command("list")
def list_ports(
    dev: bool = typer.Option(False, "--dev", "-d", help="Show developer stack ports only (hide SYS)."),
    exposed: bool = typer.Option(False, "--exposed", "-e", help="Show publicly exposed (0.0.0.0 / ::) ports only."),
    udp: bool = typer.Option(False, "--udp", help="Include UDP socket bindings."),
) -> None:
    """Display active listening ports in a crisp industrial Rich table."""
    entries, summary = scan_ports(include_udp=udp, dev_only=dev, exposed_only=exposed)

    header = Text()
    header.append(f"PORTREAPER v{__version__}", style="bold #E6EDF3")
    header.append("  │  ", style="#2A313C")
    header.append(f"{platform.system()}", style="bold #38BDF8")
    header.append("  │  ", style="#2A313C")
    header.append(f"Active: {summary.total_ports}", style="#E6EDF3")
    header.append("  │  ", style="#2A313C")
    header.append(f"Dev RAM: {summary.total_dev_ram_mb:.1f} MB", style="#10B981")
    header.append("  │  ", style="#2A313C")
    header.append(
        f"Exposed: {summary.exposed_ports}",
        style="bold #F59E0B" if summary.exposed_ports > 0 else "#10B981",
    )
    console.print(header)

    if not entries:
        console.print("[#8B949E]No matching listening ports found.[/]")
        raise typer.Exit(code=0)

    table = Table(
        box=box.SQUARE,
        border_style="#2A313C",
        header_style="bold #8B949E",
        expand=False,
        pad_edge=True,
    )
    table.add_column("PORT", justify="right", style="bold #E6EDF3", no_wrap=True)
    table.add_column("PROTO", justify="center", style="#8B949E", no_wrap=True)
    table.add_column("BIND", no_wrap=True)
    table.add_column("TAG", no_wrap=True)
    table.add_column("PROCESS", style="#E6EDF3", no_wrap=True)
    table.add_column("PID", justify="right", style="#8B949E", no_wrap=True)
    table.add_column("RAM", justify="right", no_wrap=True)
    table.add_column("CPU", justify="right", no_wrap=True)
    table.add_column("UPTIME", justify="right", style="#8B949E", no_wrap=True)

    for entry in entries:
        bind_cell = (
            Text(f"⚠ {entry.bind_addr}", style="bold #F59E0B")
            if entry.is_public_exposed
            else Text(f"● {entry.bind_addr}", style="#10B981")
        )
        tag_cell = Text(entry.dev_tag, style=TAG_COLORS.get(entry.dev_tag, "#8B949E"))
        ram_style = "#F59E0B" if entry.memory_mb >= 512.0 else "#E6EDF3"
        cpu_style = "#EF4444" if entry.cpu_percent >= 50.0 else ("#F59E0B" if entry.cpu_percent >= 15.0 else "#8B949E")

        table.add_row(
            str(entry.port),
            entry.protocol,
            bind_cell,
            tag_cell,
            entry.name,
            entry.pid_display,
            Text(f"{entry.memory_mb:.1f} MB", style=ram_style),
            Text(f"{entry.cpu_percent:.1f}%", style=cpu_style),
            entry.uptime_str,
        )

    console.print(table)


@app.command("kill")
def kill_ports_cmd(
    ports: list[int] = typer.Argument(..., help="One or more port numbers to terminate (e.g. 3000 5173)."),
    force: bool = typer.Option(False, "--force", "-f", help="Force kill (SIGKILL) and clean up recursive child process tree."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Terminate processes listening on one or more ports."""
    for p in ports:
        if p < 1 or p > 65535:
            console.print(f"[bold #EF4444]✖ Invalid port number:[/] {p} (must be 1–65535)")
            raise typer.Exit(code=2)

    if not yes:
        mode = "FORCE KILL (process tree)" if force else "gracefully stop"
        port_list = ", ".join(str(p) for p in ports)
        confirmed = typer.confirm(f"Proceed to {mode} process(es) on port(s): {port_list}?", default=False)
        if not confirmed:
            console.print("[#8B949E]Aborted.[/]")
            raise typer.Exit(code=0)

    any_failed = False
    for port in ports:
        results = kill_port(port, force=force)
        for res in results:
            if res.success:
                console.print(f"[bold #10B981]✔ [:{port}][/] {res.message}")
            else:
                any_failed = True
                console.print(f"[bold #EF4444]✖ [:{port}][/] {res.message}")

    if any_failed:
        raise typer.Exit(code=1)


def _add_tree_nodes(rich_tree: Tree, node: ProcessNode) -> None:
    label = Text()
    label.append(f"{node.name}", style="bold #E6EDF3")
    label.append(f" (PID {node.pid})", style="#8B949E")
    label.append(f"  {node.memory_mb:.1f} MB", style="#10B981")
    label.append(f"  {node.cpu_percent:.1f}% CPU", style="#F59E0B")
    label.append(f"  [{node.status}]", style="#8B949E")
    branch = rich_tree.add(label)
    for child in node.children:
        _add_tree_nodes(branch, child)


@app.command("inspect")
def inspect_port(
    port: int = typer.Argument(..., help="Port number to inspect in detail."),
) -> None:
    """Print full process tree, command line, CWD, and bindings for a port."""
    if port < 1 or port > 65535:
        console.print(f"[bold #EF4444]✖ Invalid port number:[/] {port}")
        raise typer.Exit(code=2)

    entries = get_entries_for_port(port, include_udp=True)
    if not entries:
        console.print(f"[#8B949E]No active process listening on port {port}.[/]")
        raise typer.Exit(code=1)

    for entry in entries:
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold #8B949E", no_wrap=True)
        info.add_column(style="#E6EDF3")

        bindings = ", ".join(entry.all_bind_addrs) if entry.all_bind_addrs else entry.bind_addr
        exposure = (
            Text(f"⚠ PUBLIC EXPOSED ({bindings})", style="bold #F59E0B")
            if entry.is_public_exposed
            else Text(f"● LOCALHOST ONLY ({bindings})", style="bold #10B981")
        )
        parent_desc = (
            f"{entry.parent_name} (PID {entry.parent_pid})"
            if entry.parent_pid is not None and entry.parent_name
            else (str(entry.parent_pid) if entry.parent_pid is not None else "—")
        )

        info.add_row("Port / Proto", f"{entry.port} / {entry.protocol}")
        info.add_row("Network Bind", exposure)
        info.add_row("Process", f"{entry.name} (PID {entry.pid_display})")
        info.add_row("Dev Stack", Text(entry.dev_tag, style=TAG_COLORS.get(entry.dev_tag, "#8B949E")))
        info.add_row("User", entry.username)
        info.add_row("Resources", f"{entry.memory_mb:.1f} MB RAM  │  {entry.cpu_percent:.1f}% CPU  │  Uptime {entry.uptime_str}")
        info.add_row("Parent", parent_desc)
        info.add_row("Children", str(entry.children_count))
        info.add_row("CWD", entry.cwd)
        info.add_row("CMDLINE", entry.cmdline)

        border_color = "#F59E0B" if entry.is_public_exposed else "#2A313C"
        console.print(
            Panel(
                info,
                title=f"[bold #E6EDF3]PORTREAPER INSPECT [:{entry.port}][/]",
                border_style=border_color,
                box=box.SQUARE,
                padding=(1, 2),
            )
        )

        if entry.pid is not None:
            tree_root = build_process_tree(entry.pid)
            if tree_root is not None:
                rich_tree = Tree(
                    f"[bold #E6EDF3]{tree_root.name}[/] [#8B949E](PID {tree_root.pid})[/] "
                    f"[#10B981]{tree_root.memory_mb:.1f} MB[/] [#F59E0B]{tree_root.cpu_percent:.1f}% CPU[/]",
                    guide_style="#2A313C",
                )
                for child in tree_root.children:
                    _add_tree_nodes(rich_tree, child)
                console.print(
                    Panel(
                        rich_tree,
                        title="[bold #8B949E]PROCESS TREE[/]",
                        border_style="#2A313C",
                        box=box.SQUARE,
                        padding=(0, 2),
                    )
                )


@app.command("free")
def free_port_cmd(
    start_port: int = typer.Argument(3000, help="Starting port number to search upward from."),
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Host interface to probe."),
) -> None:
    """Find and print the next available TCP port."""
    try:
        available = find_next_free_port(start_port, host=host)
    except (ValueError, RuntimeError) as exc:
        console.print(f"[bold #EF4444]✖ {exc}[/]")
        raise typer.Exit(code=1)

    if sys.stdout.isatty():
        console.print(f"[bold #10B981]{available}[/]")
    else:
        print(available)


def main() -> None:
    """Console script entry point for `portreaper`."""
    try:
        app()
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


if __name__ == "__main__":
    main()
