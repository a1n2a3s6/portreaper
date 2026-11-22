"""PortReaper Interactive Terminal UI built with Textual."""

from __future__ import annotations

import platform
import sys
import webbrowser
from collections import deque
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from portreaper import __version__
from portreaper.killer import kill_pid
from portreaper.models import PortEntry, ScanSummary
from portreaper.scanner import PortScanner

SPARK_CHARS = "▁▂▃▄▅▆▇█"
HISTORY_LEN = 26

TAG_STYLES: dict[str, str] = {
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


def _resolve_tcss_path() -> Path:
    """Resolve theme.tcss both in normal installs and PyInstaller --onefile bundles."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "portreaper" / "tui" / "theme.tcss"
        if bundled.is_file():
            return bundled
    return Path(__file__).resolve().parent / "theme.tcss"


def _fuzzy_match(query: str, text: str) -> bool:
    """Fast case-insensitive substring or subsequence fuzzy match."""
    q = query.strip().lower()
    if not q:
        return True
    t = text.lower()
    if q in t:
        return True
    it = iter(t)
    return all(ch in it for ch in q)


def _render_sparkline(samples: deque[float] | None, *, color: str, width: int = HISTORY_LEN) -> Text:
    """Render a crisp fixed-width ASCII/Unicode sparkline for CPU or RAM history."""
    if not samples:
        return Text("─" * width, style="#2A313C")

    vals = list(samples)[-width:]
    hi = max(vals) if vals else 0.0
    lo = min(vals) if vals else 0.0
    span = hi - lo

    chars: list[str] = []
    for v in vals:
        if hi <= 0:
            idx = 0
        elif span < 1e-6:
            idx = 3
        else:
            norm = (v - lo) / span
            idx = min(len(SPARK_CHARS) - 1, max(0, int(round(norm * (len(SPARK_CHARS) - 1)))))
        chars.append(SPARK_CHARS[idx])

    pad = "·" * max(0, width - len(chars))
    out = Text()
    if pad:
        out.append(pad, style="#2A313C")
    out.append("".join(chars), style=color)
    return out


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[: max(1, max_len - 1)] + "…"


class KillConfirmModal(ModalScreen[bool]):
    """Fast keyboard-first y/n modal for confirming process termination."""

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("Y", "confirm", "Yes", show=False),
        Binding("enter", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("N", "cancel", "No", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, entry: PortEntry, *, force: bool = False) -> None:
        super().__init__()
        self.entry = entry
        self.force = force

    def compose(self) -> ComposeResult:
        mode_class = "force" if self.force else "graceful"
        action_label = "FORCE KILL PROCESS TREE (SIGKILL)" if self.force else "GRACEFUL STOP (SIGTERM)"
        tree_info = (
            f" + {self.entry.children_count} child worker(s)"
            if self.force and self.entry.children_count > 0
            else ""
        )

        body = Text()
        body.append("Target  : ", style="#8B949E")
        body.append(f"{self.entry.name}", style="bold #E6EDF3")
        body.append(f"  (PID {self.entry.pid_display}{tree_info})\n", style="#8B949E")
        body.append("Port    : ", style="#8B949E")
        body.append(f"{self.entry.protocol}:{self.entry.port}", style="bold #10B981")
        body.append(f"  [{self.entry.bind_addr}]\n", style="#F59E0B" if self.entry.is_public_exposed else "#8B949E")
        body.append("Command : ", style="#8B949E")
        body.append(_truncate(self.entry.cmdline, 52), style="#E6EDF3")

        hint = Text()
        hint.append("[y]", style="bold #EF4444" if self.force else "bold #F59E0B")
        hint.append(" Confirm   ", style="#E6EDF3")
        hint.append("[n / Esc]", style="bold #8B949E")
        hint.append(" Cancel", style="#8B949E")

        with Vertical(id="kill-modal-box", classes=mode_class):
            yield Static(f"⚠ {action_label}", id="kill-modal-title")
            yield Static(body, id="kill-modal-body")
            yield Static(hint, id="kill-modal-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PortReaperApp(App[None]):
    """Cross-platform industrial Port & Dev Process Sniper TUI."""

    CSS_PATH = str(_resolve_tcss_path())
    TITLE = f"PORTREAPER v{__version__}"

    # Note: Up/Down arrows and `j` navigate rows; `k` triggers graceful-kill (with y/n modal)
    # and `K` triggers recursive force-kill as specified, avoiding keybinding collision on `k`.
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("g", "cursor_top", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("slash", "focus_search", "Filter", show=False),
        Binding("d", "toggle_dev_only", "Dev Only", show=False),
        Binding("p", "toggle_pause", "Pause", show=False),
        Binding("k", "graceful_kill", "Kill", show=False),
        Binding("K", "force_kill", "Force Tree", show=False),
        Binding("o", "open_browser", "Open", show=False),
        Binding("r", "force_refresh", "Refresh", show=False),
        Binding("enter", "toggle_inspector", "Inspector", show=False),
        Binding("escape", "clear_or_blur_search", "Esc", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    dev_only: reactive[bool] = reactive(False)
    paused: reactive[bool] = reactive(False)
    inspector_open: reactive[bool] = reactive(True)
    filter_query: reactive[str] = reactive("")

    def __init__(self, *, dev_only: bool = False, include_udp: bool = False) -> None:
        super().__init__()
        self._scanner = PortScanner()
        self._include_udp = include_udp
        self._all_entries: list[PortEntry] = []
        self._visible_entries: list[PortEntry] = []
        self._entry_by_key: dict[str, PortEntry] = {}
        self._summary = ScanSummary(0, 0, 0, 0.0, 0)
        self._selected_key: str | None = None
        self._ram_history: dict[int, deque[float]] = {}
        self._cpu_history: dict[int, deque[float]] = {}
        self._os_label = "Windows" if platform.system() == "Windows" else platform.system() or "Linux"
        self.dev_only = dev_only

    def compose(self) -> ComposeResult:
        yield Static("", id="status-bar")
        with Container(id="search-container"):
            yield Input(
                placeholder="Filter by port, process, tag, bind, or cmdline… (Esc to close)",
                id="search-input",
            )
        with Container(id="table-container"):
            yield DataTable(id="ports-table", cursor_type="row")
        with Container(id="inspector-drawer"):
            yield Static("", id="inspector-content")
        yield Static("", id="keybar")

    def on_mount(self) -> None:
        table = self.query_one("#ports-table", DataTable)
        table.zebra_stripes = False
        table.add_column("PORT", key="port", width=8)
        table.add_column("BIND", key="bind", width=16)
        table.add_column("TAG", key="tag", width=10)
        table.add_column("PROCESS", key="process", width=22)
        table.add_column("PID", key="pid", width=8)
        table.add_column("RAM", key="ram", width=10)
        table.add_column("CPU", key="cpu", width=8)
        table.add_column("UPTIME", key="uptime", width=10)

        self._render_keybar()
        self._refresh_data()
        table.focus()
        self.set_interval(1.5, self._on_tick)

    def _on_tick(self) -> None:
        if not self.paused:
            self._refresh_data()

    def _refresh_data(self) -> None:
        entries, summary = self._scanner.scan(include_udp=self._include_udp)
        self._all_entries = entries
        self._summary = summary

        # Record rolling RAM/CPU history per PID for live inspector sparklines.
        seen_pids: set[int] = set()
        for entry in entries:
            if entry.pid is not None and entry.pid not in seen_pids:
                seen_pids.add(entry.pid)
                self._ram_history.setdefault(entry.pid, deque(maxlen=HISTORY_LEN)).append(entry.memory_mb)
                self._cpu_history.setdefault(entry.pid, deque(maxlen=HISTORY_LEN)).append(entry.cpu_percent)

        # Clean up exited PID histories.
        for pid in list(self._ram_history.keys()):
            if pid not in seen_pids:
                self._ram_history.pop(pid, None)
                self._cpu_history.pop(pid, None)

        self._apply_filter_and_render()

   def _apply_filter_and_render(self) -> None:
        if not self.is_mounted:
            return
        filtered = self._all_entries
        if self.dev_only:
            filtered = [e for e in filtered if e.is_dev]
        if self.filter_query.strip():
            q = self.filter_query.strip()
            filtered = [
                e
                for e in filtered
                if _fuzzy_match(q, f"{e.port} {e.protocol} {e.bind_addr} {e.dev_tag} {e.name} {e.cmdline}")
            ]
        self._visible_entries = filtered
        self._entry_by_key = {e.row_key: e for e in filtered}

        table = self.query_one("#ports-table", DataTable)
        prev_key = self._selected_key

        table.clear(columns=False)
        for entry in filtered:
            port_text = Text(f"{entry.port}", style="bold #E6EDF3")
            if entry.protocol == "UDP":
                port_text.append("u", style="#8B949E")

            if entry.is_public_exposed:
                bind_text = Text(f"⚠ {entry.bind_addr}", style="bold #F59E0B")
            else:
                bind_text = Text(f"● {entry.bind_addr}", style="#10B981")

            tag_style = TAG_STYLES.get(entry.dev_tag, "#8B949E")
            tag_text = Text(entry.dev_tag, style=tag_style)

            proc_text = Text(_truncate(entry.name, 21), style="#E6EDF3" if entry.is_dev else "#8B949E")
            pid_text = Text(entry.pid_display, style="#8B949E")
            ram_style = "#F59E0B" if entry.memory_mb >= 512.0 else "#E6EDF3"
            ram_text = Text(f"{entry.memory_mb:.1f} MB", style=ram_style)
            cpu_style = "#EF4444" if entry.cpu_percent >= 50.0 else ("#F59E0B" if entry.cpu_percent >= 15.0 else "#8B949E")
            cpu_text = Text(f"{entry.cpu_percent:.1f}%", style=cpu_style)
            uptime_text = Text(entry.uptime_str, style="#8B949E")

            table.add_row(
                port_text,
                bind_text,
                tag_text,
                proc_text,
                pid_text,
                ram_text,
                cpu_text,
                uptime_text,
                key=entry.row_key,
            )

        # Restore cursor selection cleanly across auto-refreshes.
        if filtered:
            target_idx = 0
            if prev_key is not None:
                for idx, entry in enumerate(filtered):
                    if entry.row_key == prev_key:
                        target_idx = idx
                        break
            self._selected_key = filtered[target_idx].row_key
            table.move_cursor(row=target_idx, animate=False)
        else:
            self._selected_key = None

        self._render_status_bar()
        self._render_inspector()

    def _render_status_bar(self) -> None:
        bar = Text()
        bar.append(f" PORTREAPER v{__version__} ", style="bold #0B0E14 on #E6EDF3")
        bar.append(" │ ", style="#2A313C")
        bar.append(f"{self._os_label}", style="bold #38BDF8")
        bar.append(" │ ", style="#2A313C")
        bar.append("PORTS ", style="#8B949E")
        bar.append(f"{self._summary.total_ports}", style="bold #E6EDF3")
        if self.dev_only:
            bar.append(f" ({len(self._visible_entries)} dev)", style="#10B981")
        bar.append(" │ ", style="#2A313C")
        bar.append("DEV RAM ", style="#8B949E")
        bar.append(f"{self._summary.total_dev_ram_mb:.1f} MB", style="bold #10B981")
        bar.append(" │ ", style="#2A313C")
        if self._summary.exposed_ports > 0:
            bar.append(f"⚠ EXPOSED {self._summary.exposed_ports}", style="bold #F59E0B")
        else:
            bar.append("EXPOSED 0", style="#10B981")

        if self.dev_only:
            bar.append(" │ ", style="#2A313C")
            bar.append("DEV-ONLY", style="bold #0B0E14 on #10B981")
        if self.paused:
            bar.append(" │ ", style="#2A313C")
            bar.append("PAUSED", style="bold #0B0E14 on #F59E0B")
        if self.filter_query.strip():
            bar.append(" │ ", style="#2A313C")
            bar.append(f"FILTER: {self.filter_query.strip()}", style="bold #F59E0B")

        self.query_one("#status-bar", Static).update(bar)

    def _render_keybar(self) -> None:
        hints = [
            ("↑/↓/j", "Nav"),
            ("/", "Filter"),
            ("d", "Dev-Only"),
            ("p", "Pause"),
            ("k", "Stop"),
            ("K", "Force Tree"),
            ("o", "Open"),
            ("Enter", "Inspector"),
            ("r", "Refresh"),
            ("q", "Quit"),
        ]
        line = Text()
        for idx, (key, label) in enumerate(hints):
            if idx > 0:
                line.append("  ", style="#2A313C")
            key_color = "#EF4444" if key in {"k", "K"} else "#E6EDF3"
            line.append(f"[{key}]", style=f"bold {key_color}")
            line.append(f" {label}", style="#8B949E")
        self.query_one("#keybar", Static).update(line)

    def _get_selected_entry(self) -> PortEntry | None:
        if self._selected_key and self._selected_key in self._entry_by_key:
            return self._entry_by_key[self._selected_key]
        if self._visible_entries:
            return self._visible_entries[0]
        return None

    def _render_inspector(self) -> None:
        widget = self.query_one("#inspector-content", Static)
        entry = self._get_selected_entry()
        if entry is None:
            empty = Text("No matching listening ports. Press [d] to toggle Dev-Only or [/] to clear filter.", style="#8B949E")
            widget.update(empty)
            return

        ram_hist = self._ram_history.get(entry.pid) if entry.pid is not None else None
        cpu_hist = self._cpu_history.get(entry.pid) if entry.pid is not None else None

        panel = Text()
        # Line 1: Header + Exposure status
        panel.append("INSPECTOR  ", style="bold #8B949E")
        panel.append(f"{entry.name}", style="bold #E6EDF3")
        panel.append(f"  PID {entry.pid_display}", style="#8B949E")
        panel.append(f"  [{entry.dev_tag}]", style=f" {TAG_STYLES.get(entry.dev_tag, '#8B949E')}")
        panel.append("  │  ", style="#2A313C")
        if entry.is_public_exposed:
            panel.append(f"PUBLIC EXPOSED ({', '.join(entry.all_bind_addrs or [entry.bind_addr])})", style="bold #F59E0B")
        else:
            panel.append(f"LOCALHOST ({', '.join(entry.all_bind_addrs or [entry.bind_addr])})", style="#10B981")
        panel.append("\n")

        # Line 2: CMDLINE
        panel.append("CMDLINE  ", style="bold #8B949E")
        panel.append(_truncate(entry.cmdline, 118), style="#E6EDF3")
        panel.append("\n")

        # Line 3: CWD
        panel.append("CWD      ", style="bold #8B949E")
        panel.append(_truncate(entry.cwd, 118), style="#E6EDF3")
        panel.append("\n")

        # Line 4: Process Tree & User
        parent_str = (
            f"{entry.parent_name} ({entry.parent_pid})"
            if entry.parent_pid is not None and entry.parent_name
            else (str(entry.parent_pid) if entry.parent_pid is not None else "—")
        )
        panel.append("TREE     ", style="bold #8B949E")
        panel.append(f"Parent: {parent_str}", style="#E6EDF3")
        panel.append("  │  ", style="#2A313C")
        panel.append(f"Children: {entry.children_count}", style="bold #F59E0B" if entry.children_count > 0 else "#E6EDF3")
        panel.append("  │  ", style="#2A313C")
        panel.append(f"User: {entry.username}", style="#8B949E")
        panel.append("\n")

        # Line 5: Live RAM sparkline
        panel.append("RAM      ", style="bold #8B949E")
        panel.append(f"{entry.memory_mb:6.1f} MB  ", style="bold #10B981")
        panel.append_text(_render_sparkline(ram_hist, color="#10B981"))
        panel.append("   ")

        # Live CPU sparkline on same or next line
        panel.append("CPU  ", style="bold #8B949E")
        panel.append(f"{entry.cpu_percent:5.1f}%  ", style="bold #F59E0B")
        panel.append_text(_render_sparkline(cpu_hist, color="#F59E0B"))

        widget.update(panel)

    # --- Event Handlers ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._selected_key = str(event.row_key.value)
            self._render_inspector()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._selected_key = str(event.row_key.value)
        self.action_toggle_inspector()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self.filter_query = event.value
            self._apply_filter_and_render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search-input":
            self.query_one("#ports-table", DataTable).focus()

    def on_key(self, event: events.Key) -> None:
        # While typing in the search box, only Escape returns focus to the table.
        search_input = self.query_one("#search-input", Input)
        if search_input.has_focus and event.key == "escape":
            event.stop()
            self.action_clear_or_blur_search()

   # --- Reactive Watchers ---

    def watch_dev_only(self, _: bool) -> None:
        if self.is_mounted and self.screen_stack:
            self._apply_filter_and_render()

    def watch_paused(self, _: bool) -> None:
        if self.is_mounted and self.screen_stack:
            self._render_status_bar()

    def watch_inspector_open(self, is_open: bool) -> None:
        if self.is_mounted and self.screen_stack:
            drawer = self.query_one("#inspector-drawer")
            drawer.set_class(not is_open, "collapsed")
    # --- Actions ---

    def _is_search_focused(self) -> bool:
        return self.query_one("#search-input", Input).has_focus

    def action_cursor_down(self) -> None:
        if self._is_search_focused():
            return
        self.query_one("#ports-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        if self._is_search_focused():
            return
        self.query_one("#ports-table", DataTable).action_cursor_up()

    def action_cursor_top(self) -> None:
        if self._is_search_focused():
            return
        table = self.query_one("#ports-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=0, animate=False)

    def action_cursor_bottom(self) -> None:
        if self._is_search_focused():
            return
        table = self.query_one("#ports-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1, animate=False)

    def action_focus_search(self) -> None:
        container = self.query_one("#search-container")
        container.add_class("visible")
        search_input = self.query_one("#search-input", Input)
        search_input.focus()

    def action_clear_or_blur_search(self) -> None:
        search_input = self.query_one("#search-input", Input)
        container = self.query_one("#search-container")
        if search_input.value:
            search_input.value = ""
            self.filter_query = ""
            self._apply_filter_and_render()
        container.remove_class("visible")
        self.query_one("#ports-table", DataTable).focus()

    def action_toggle_dev_only(self) -> None:
        if self._is_search_focused():
            return
        self.dev_only = not self.dev_only
        state = "ON (hiding SYS ports)" if self.dev_only else "OFF (showing all ports)"
        self.notify(f"Dev Ports Only: {state}", severity="information", timeout=2.0)

    def action_toggle_pause(self) -> None:
        if self._is_search_focused():
            return
        self.paused = not self.paused
        state = "PAUSED" if self.paused else "RESUMED (1.5s)"
        self.notify(f"Auto-refresh {state}", severity="warning" if self.paused else "information", timeout=2.0)

    def action_toggle_inspector(self) -> None:
        if self._is_search_focused():
            return
        self.inspector_open = not self.inspector_open
        if self.inspector_open:
            self._render_inspector()

    def action_force_refresh(self) -> None:
        if self._is_search_focused():
            return
        self._refresh_data()
        self.notify("Port table refreshed", severity="information", timeout=1.5)

    def action_open_browser(self) -> None:
        if self._is_search_focused():
            return
        entry = self._get_selected_entry()
        if entry is None:
            self.notify("No port selected", severity="warning", timeout=2.0)
            return
        url = f"http://localhost:{entry.port}"
        try:
            webbrowser.open(url)
            self.notify(f"Opened {url}", severity="information", timeout=2.5)
        except Exception as exc:
            self.notify(f"Failed to open browser: {exc}", severity="error", timeout=3.0)

    def action_graceful_kill(self) -> None:
        if self._is_search_focused():
            return
        self._prompt_kill(force=False)

    def action_force_kill(self) -> None:
        if self._is_search_focused():
            return
        self._prompt_kill(force=True)

    def _prompt_kill(self, *, force: bool) -> None:
        entry = self._get_selected_entry()
        if entry is None:
            self.notify("No port selected", severity="warning", timeout=2.0)
            return

        def _handle_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            result = kill_pid(entry.pid, force=force, port=entry.port)
            if result.success:
                self.notify(result.message, severity="information", timeout=3.5)
            else:
                self.notify(result.message, severity="error", timeout=4.5)
            self._refresh_data()

        self.push_screen(KillConfirmModal(entry, force=force), _handle_confirm)


def run_tui(*, dev_only: bool = False, include_udp: bool = False) -> None:
    """Launch the interactive PortReaper TUI."""
    app = PortReaperApp(dev_only=dev_only, include_udp=include_udp)
    app.run()
