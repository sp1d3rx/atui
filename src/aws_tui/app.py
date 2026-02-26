from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Log, Select, Static
from textual.worker import Worker, WorkerState

if __package__ in (None, ""):
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from aws_tui.aws_api import (
        DEFAULT_PROFILE,
        DEFAULT_REGION,
        AwsEc2Service,
        AwsInstance,
        build_mock_instances,
        is_aws_cli_available,
    )
    from aws_tui.models import InstanceSummary
    from aws_tui.port_config import PortForwardConfig, load_port_forward_config
    from aws_tui.port_history import (
        DEFAULT_HISTORY_DB_PATH,
        PortForwardHistoryStore,
        PortForwardRecord,
        utc_now,
    )
else:
    from .aws_api import (
        DEFAULT_PROFILE,
        DEFAULT_REGION,
        AwsEc2Service,
        AwsInstance,
        build_mock_instances,
        is_aws_cli_available,
    )
    from .models import InstanceSummary
    from .port_config import PortForwardConfig, load_port_forward_config
    from .port_history import (
        DEFAULT_HISTORY_DB_PATH,
        PortForwardHistoryStore,
        PortForwardRecord,
        utc_now,
    )


@dataclass(slots=True)
class ActivePortForwardRuntime:
    record_id: str
    instance_id: str
    process: subprocess.Popen[bytes] | None
    simulated: bool = False
    stopping: bool = False


class PortForwardScreen(ModalScreen[tuple[str, int, int] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, config: PortForwardConfig) -> None:
        super().__init__()
        self.config = config
        self.preset_by_key = {preset.key: preset for preset in self.config.presets}

    def compose(self) -> ComposeResult:
        with Vertical(id="port-modal"):
            yield Label("Add SSM Port Forwarding", id="port-modal-title")
            yield Label("Forward name")
            yield Input(
                value=f"forward-{self.config.default_local_port}-to-{self.config.default_remote_port}",
                id="forward-name",
            )
            yield Label("Preset ports")
            yield Select(
                [("Custom", "custom"), *[(preset.label, preset.key) for preset in self.config.presets]],
                allow_blank=False,
                value="custom",
                id="port-preset",
            )
            yield Label("Remote port on selected EC2 instance")
            yield Input(value=str(self.config.default_remote_port), id="remote-port")
            yield Label("Local port on this machine")
            yield Input(value=str(self.config.default_local_port), id="local-port")
            with Horizontal(id="port-modal-buttons"):
                yield Button("Cancel", id="cancel-port")
                yield Button("Add", variant="primary", id="add-port")

    @on(Select.Changed, "#port-preset")
    def on_preset_changed(self, event: Select.Changed) -> None:
        if event.value == Select.BLANK:
            return
        try:
            preset = self.preset_by_key[str(event.value)]
        except KeyError:
            return
        self.query_one("#forward-name", Input).value = _name_from_preset_label(preset.label)
        self.query_one("#remote-port", Input).value = str(preset.remote_port)
        self.query_one("#local-port", Input).value = str(preset.local_port)

    async def action_cancel(self) -> None:
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-port":
            self.dismiss(None)
            return
        forward_name = self.query_one("#forward-name", Input).value.strip()
        if not forward_name:
            self.app.notify("Forward name is required.", severity="error")
            return
        remote_port = _parse_port(self.query_one("#remote-port", Input).value.strip())
        local_port = _parse_port(self.query_one("#local-port", Input).value.strip())
        if remote_port is None or local_port is None:
            self.app.notify("Ports must be between 1 and 65535", severity="error")
            return
        self.dismiss((forward_name, remote_port, local_port))


class InstanceInfoScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("a", "add_forward", "Add"),
        Binding("s", "start_selected", "Start"),
        Binding("x", "stop_selected", "Stop"),
    ]

    def __init__(self, instance: InstanceSummary) -> None:
        super().__init__()
        self.instance = instance
        self.active_records: list[PortForwardRecord] = []
        self.history_records: list[PortForwardRecord] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="instance-info-modal"):
            yield Label(
                f"{self.instance.display_name} ({self.instance.instance_id})",
                id="instance-info-title",
            )
            yield Static(self._instance_meta_text(), id="instance-info-meta")
            with Horizontal(id="instance-info-actions"):
                yield Button("Add forward", variant="primary", id="info-add")
                yield Button("Start selected", id="info-start")
                yield Button("Stop selected", id="info-stop")
                yield Button("Close", id="info-close")
            yield Label("Active Port Forwards (started from this app)")
            yield DataTable(id="active-forwards-table")
            yield Label("Port Forward History")
            yield DataTable(id="forward-history-table")

    def on_mount(self) -> None:
        active_table = self.query_one("#active-forwards-table", DataTable)
        active_table.cursor_type = "row"
        active_table.add_columns("Forward Name", "Local", "Remote", "Status", "Started", "Command")

        history_table = self.query_one("#forward-history-table", DataTable)
        history_table.cursor_type = "row"
        history_table.add_columns("Forward Name", "Local", "Remote", "Status", "Started", "Ended")
        self.action_refresh()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "info-add":
                self.action_add_forward()
            case "info-start":
                self.action_start_selected()
            case "info-stop":
                self.action_stop_selected()
            case "info-close":
                self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_add_forward(self) -> None:
        app = cast(AwsTuiApp, self.app)
        app.prompt_port_forward_for_instance(self.instance, on_complete=self.action_refresh)

    def action_start_selected(self) -> None:
        record = self._selected_history_record()
        if record is None:
            self.app.notify("Select a history record to start.", severity="warning")
            return
        if record.status in {"active", "simulated-active"}:
            self.app.notify("That forward is already active.", severity="warning")
            return
        app = cast(AwsTuiApp, self.app)
        app.start_port_forward(
            self.instance,
            remote_port=record.remote_port,
            local_port=record.local_port,
            forward_name=record.forward_name,
        )
        self.action_refresh()

    def action_stop_selected(self) -> None:
        record = self._selected_active_record()
        if record is None:
            self.app.notify("Select an active forward to stop.", severity="warning")
            return
        app = cast(AwsTuiApp, self.app)
        app.stop_port_forward(record.record_id)
        self.action_refresh()

    def action_refresh(self) -> None:
        app = cast(AwsTuiApp, self.app)
        active = app.get_active_forwards_for_instance(self.instance.instance_id)
        history = app.get_history_for_instance(self.instance.instance_id)

        self.active_records = active
        self.history_records = history
        active_table = self.query_one("#active-forwards-table", DataTable)
        active_table.clear(columns=False)
        for record in active:
            active_table.add_row(
                record.forward_name,
                str(record.local_port),
                str(record.remote_port),
                record.status,
                _format_timestamp(record.started_at),
                _truncate(record.command, 58),
            )
        if active:
            active_table.move_cursor(row=0, column=0)

        history_table = self.query_one("#forward-history-table", DataTable)
        history_table.clear(columns=False)
        for record in history:
            history_table.add_row(
                record.forward_name,
                str(record.local_port),
                str(record.remote_port),
                record.status,
                _format_timestamp(record.started_at),
                _format_timestamp(record.ended_at),
            )
        if history:
            history_table.move_cursor(row=0, column=0)

    def _selected_active_record(self) -> PortForwardRecord | None:
        table = self.query_one("#active-forwards-table", DataTable)
        try:
            row = table.cursor_row
            if row < 0:
                raise IndexError
            return self.active_records[row]
        except IndexError:
            return None

    def _selected_history_record(self) -> PortForwardRecord | None:
        table = self.query_one("#forward-history-table", DataTable)
        try:
            row = table.cursor_row
            if row < 0:
                raise IndexError
            return self.history_records[row]
        except IndexError:
            return None

    def _instance_meta_text(self) -> str:
        return (
            f"State: {self.instance.state} | Type: {self.instance.instance_type} | "
            f"Private IP: {self.instance.private_ip or '-'} | Public IP: {self.instance.public_ip or '-'}"
        )


class QuitConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, active_records: list[PortForwardRecord]) -> None:
        super().__init__()
        self.active_records = active_records

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-modal"):
            yield Label("Exit AWS TUI?", id="quit-modal-title")
            active_count = len(self.active_records)
            suffix = "s" if active_count != 1 else ""
            yield Static(
                f"{active_count} active port forward{suffix} will be stopped before exit.",
                id="quit-modal-body",
            )
            yield DataTable(id="quit-active-table")
            with Horizontal(id="quit-modal-buttons"):
                yield Button("Cancel", id="quit-cancel")
                yield Button("Stop & Exit", variant="error", id="quit-confirm")

    def on_mount(self) -> None:
        table = self.query_one("#quit-active-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Forward", "Machine", "Local", "Remote", "Status")
        for record in self.active_records:
            table.add_row(
                record.forward_name,
                f"{record.instance_name} ({record.instance_id})",
                str(record.local_port),
                str(record.remote_port),
                record.status,
            )
        if self.active_records:
            table.move_cursor(row=0, column=0)

    async def action_cancel(self) -> None:
        self.dismiss(False)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit-confirm":
            self.dismiss(True)
            return
        self.dismiss(False)


class AwsTuiApp(App[None]):
    CSS_PATH = "styles.tcss"
    TITLE = "AWS EC2 TUI"
    SUB_TITLE = "SSM connect + port forwarding"
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("c", "connect", "Connect (SSM)"),
        Binding("p", "port_forward", "Port map"),
        Binding("y", "copy_command", "Copy cmd"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        profile: str = DEFAULT_PROFILE,
        region: str = DEFAULT_REGION,
        ports_config: str | None = None,
        history_file: str | None = None,
    ) -> None:
        super().__init__()
        self.profile = profile or DEFAULT_PROFILE
        self.region = region or DEFAULT_REGION
        self.aws_cli_available = is_aws_cli_available()
        self.port_forward_config = load_port_forward_config(ports_config)
        self.history_store = PortForwardHistoryStore(history_file)
        self.active_port_forwards: dict[str, ActivePortForwardRuntime] = {}
        self.instances: list[InstanceSummary] = []
        self.current_command = ""
        self.exit_in_progress = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="settings-bar"):
            yield Label("Profile")
            yield Input(value=self.profile, id="profile")
            yield Label("Region")
            yield Input(value=self.region, id="region")
            yield Button("Refresh", variant="primary", id="refresh")
            yield Button("Connect (SSM)", id="connect")
            yield Button("Port map", id="port-map")
        yield DataTable(id="instance-table")
        yield Static("Loading instances...", id="status")
        with Horizontal(id="command-bar"):
            yield Label("Command", id="command-label")
            yield Input(
                value="",
                placeholder="Select an EC2 instance to preview command...",
                id="command-preview",
            )
            yield Button("Copy", id="copy-command")
        yield Log(highlight=False, max_lines=500, auto_scroll=True, id="activity-log")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#instance-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Instance ID", "State", "Type", "Private IP", "Public IP", "AZ")
        self.set_interval(1.0, self._poll_active_port_forwards)

        if not self.aws_cli_available:
            self._log("AWS CLI not found. Running in simulated mode.")
            self.notify("AWS CLI not found; simulated mode is active.", severity="warning")
        self._log(f"Port-forward history file: {self.history_store.path}")
        self._log("Press Enter on an instance to open details (Add new, Start stopped, Stop active).")
        self._log("App started.")
        self.set_focus(table)
        self.action_refresh()

    @work(thread=True, exclusive=True, name="load-instances")
    def load_instances(self, profile: str, region: str) -> list[InstanceSummary]:
        if not self.aws_cli_available:
            return build_mock_instances(region=region)
        return AwsEc2Service(profile=profile, region=region).list_instances()

    @on(Worker.StateChanged)
    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "load-instances":
            return

        if event.worker.state == WorkerState.SUCCESS:
            result = event.worker.result
            if isinstance(result, list):
                self.instances = cast(list[InstanceSummary], result)
                self._render_instances()
                mode = "simulated " if not self.aws_cli_available else ""
                self._set_status(
                    f"Loaded {len(self.instances)} {mode}instances from {self.region} ({self.profile})."
                )
                self._log(
                    f"Loaded {len(self.instances)} {mode}instances from {self.region} ({self.profile})."
                )
            return

        if event.worker.state == WorkerState.ERROR:
            self.instances = []
            self._render_instances()
            error = event.worker.error
            self._set_status(f"Failed to load instances: {error}")
            self._log(f"Failed to load instances: {error}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "refresh":
                self.action_refresh()
            case "connect":
                await self.action_connect()
            case "port-map":
                self.action_port_forward()
            case "copy-command":
                self.action_copy_command()

    async def action_quit(self) -> None:
        if self.exit_in_progress:
            return

        active_records = self.get_all_active_forwards()
        if not active_records:
            self.exit()
            return

        self.push_screen(
            QuitConfirmScreen(active_records),
            callback=self._on_quit_confirmation,
        )

    def _on_quit_confirmation(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._log("Quit cancelled; active port forwards are still running.")
            return

        self.exit_in_progress = True
        self.notify("Stopping active port forwards before exit...", severity="warning")
        self._log(f"Stopping {len(self.active_port_forwards)} active port forward(s) before exit.")
        self.shutdown_active_port_forwards(emit_ui=False)
        self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in {"profile", "region"}:
            self.action_refresh()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "instance-table":
            self._update_command_preview_for_selection()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "instance-table":
            return
        instance = self._selected_instance()
        if instance is None:
            return
        self.push_screen(InstanceInfoScreen(instance))

    def action_refresh(self) -> None:
        self.profile, self.region = self._current_settings()
        self._set_status(f"Loading instances from {self.region} ({self.profile})...")
        self._set_command_preview("")
        self._log(f"Refreshing instances for {self.region} ({self.profile}).")
        self.load_instances(self.profile, self.region)

    async def action_connect(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            self.notify("Select an EC2 instance first", severity="warning")
            self._log("Connect requested with no selected instance.")
            return

        command = AwsInstance(
            instance_id=instance.instance_id,
            profile=self.profile,
            region=self.region,
        ).build_ssm_shell_command()
        self._show_command(command)
        if not self.aws_cli_available:
            self._set_status("Simulated SSM session (AWS CLI not installed).")
            self._log(f"Simulated SSM session for {instance.instance_id}.")
            return

        self._set_status(f"Starting SSM session for {instance.display_name} ({instance.instance_id})...")
        self._log(f"Starting SSM session for {instance.instance_id}.")
        with self.suspend():
            result = subprocess.run(command, check=False)
        if result.returncode == 0:
            self._set_status("SSM session ended.")
            self._log(f"SSM session ended for {instance.instance_id}.")
        else:
            self._set_status(f"SSM session exited with code {result.returncode}.")
            self._log(f"SSM session failed for {instance.instance_id} (exit {result.returncode}).")

    def action_port_forward(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            self.notify("Select an EC2 instance first", severity="warning")
            self._log("Port forwarding requested with no selected instance.")
            return
        self.prompt_port_forward_for_instance(instance)

    def prompt_port_forward_for_instance(
        self,
        instance: InstanceSummary,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        self.push_screen(
            PortForwardScreen(self.port_forward_config),
            callback=lambda mapping: self._on_port_forward_dismissed(instance, mapping, on_complete),
        )

    def _on_port_forward_dismissed(
        self,
        instance: InstanceSummary,
        port_mapping: tuple[str, int, int] | None,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        if port_mapping is None:
            self._log("Port forwarding cancelled.")
            if on_complete is not None:
                on_complete()
            return

        forward_name, remote_port, local_port = port_mapping
        self.start_port_forward(instance, remote_port, local_port, forward_name=forward_name)
        if on_complete is not None:
            on_complete()

    def start_port_forward(
        self,
        instance: InstanceSummary,
        remote_port: int,
        local_port: int,
        *,
        forward_name: str,
    ) -> PortForwardRecord | None:
        command = AwsInstance(
            instance_id=instance.instance_id,
            profile=self.profile,
            region=self.region,
        ).build_port_forward_command(remote_port=remote_port, local_port=local_port)
        self._show_command(command)
        command_text = shlex.join(command)

        if not self.aws_cli_available:
            record = self.history_store.create(
                forward_name=forward_name,
                instance_id=instance.instance_id,
                instance_name=instance.display_name,
                remote_port=remote_port,
                local_port=local_port,
                status="simulated-active",
                command=command_text,
                note="AWS CLI unavailable; simulated entry.",
            )
            self.active_port_forwards[record.record_id] = ActivePortForwardRuntime(
                record_id=record.record_id,
                instance_id=record.instance_id,
                process=None,
                simulated=True,
            )
            self._set_status("Simulated SSM port forwarding started.")
            self._log(
                f"Simulated port forward '{forward_name}' started "
                f"({local_port} -> {instance.instance_id}:{remote_port})."
            )
            return record

        self._set_status(f"Starting SSM port forwarding {local_port}-> {instance.instance_id}:{remote_port}...")
        self._log(
            f"Starting SSM port forwarding local {local_port} to {instance.instance_id}:{remote_port}."
        )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            record = self.history_store.create(
                forward_name=forward_name,
                instance_id=instance.instance_id,
                instance_name=instance.display_name,
                remote_port=remote_port,
                local_port=local_port,
                status="failed",
                command=command_text,
                note=f"Failed to start process: {error}",
            )
            self.history_store.update(record.record_id, ended_at=utc_now())
            self._set_status("Failed to start SSM port forwarding process.")
            self._log(f"Failed to start port forwarding process: {error}")
            return None

        record = self.history_store.create(
            forward_name=forward_name,
            instance_id=instance.instance_id,
            instance_name=instance.display_name,
            remote_port=remote_port,
            local_port=local_port,
            status="active",
            command=command_text,
        )
        self.active_port_forwards[record.record_id] = ActivePortForwardRuntime(
            record_id=record.record_id,
            instance_id=record.instance_id,
            process=process,
        )
        self._set_status("SSM port forwarding started in background.")
        self._log(
            f"Port forward '{forward_name}' active ({local_port} -> {instance.instance_id}:{remote_port})."
        )
        return record

    def stop_port_forward(self, record_id: str, *, emit_ui: bool = True) -> bool:
        runtime = self.active_port_forwards.get(record_id)
        record = self.history_store.get(record_id)
        if runtime is None or record is None:
            if emit_ui:
                self._log(f"Requested stop for unknown port-forward record: {record_id}.")
            return False

        if runtime.simulated or runtime.process is None:
            status = "simulated-stopped" if runtime.simulated else "stopped"
            self.history_store.update(
                record_id,
                status=status,
                ended_at=utc_now(),
                note="Stopped by user.",
            )
            self.active_port_forwards.pop(record_id, None)
            if emit_ui:
                self._set_status("Stopped selected port forward.")
                self._log(
                    f"Stopped port forward '{record.forward_name}' "
                    f"({record.local_port} -> {record.instance_id}:{record.remote_port})."
                )
            return True

        runtime.stopping = True
        self._terminate_process(runtime.process)
        exit_code = runtime.process.poll()
        status = "stopped" if exit_code in (0, None, -signal.SIGTERM, -signal.SIGKILL) else "failed"
        self.history_store.update(
            record_id,
            status=status,
            ended_at=utc_now(),
            note=f"Stopped by user (exit={exit_code}).",
        )
        self.active_port_forwards.pop(record_id, None)
        if emit_ui:
            self._set_status("Stopped selected port forward.")
            self._log(
                f"Stopped port forward '{record.forward_name}' "
                f"({record.local_port} -> {record.instance_id}:{record.remote_port})."
            )
        return True

    def get_active_forwards_for_instance(self, instance_id: str) -> list[PortForwardRecord]:
        records: list[PortForwardRecord] = []
        for runtime in self.active_port_forwards.values():
            if runtime.instance_id != instance_id:
                continue
            record = self.history_store.get(runtime.record_id)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: item.started_at, reverse=True)
        return records

    def get_all_active_forwards(self) -> list[PortForwardRecord]:
        records: list[PortForwardRecord] = []
        for runtime in self.active_port_forwards.values():
            record = self.history_store.get(runtime.record_id)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: item.started_at, reverse=True)
        return records

    def get_history_for_instance(self, instance_id: str) -> list[PortForwardRecord]:
        return self.history_store.list_for_instance(instance_id)

    def shutdown_active_port_forwards(self, *, emit_ui: bool = True) -> None:
        for record_id in tuple(self.active_port_forwards):
            self.stop_port_forward(record_id, emit_ui=emit_ui)

    def _poll_active_port_forwards(self) -> None:
        for record_id, runtime in tuple(self.active_port_forwards.items()):
            process = runtime.process
            if runtime.simulated or process is None:
                continue

            exit_code = process.poll()
            if exit_code is None:
                continue

            status = "stopped" if runtime.stopping else ("completed" if exit_code == 0 else "failed")
            self.history_store.update(
                record_id,
                status=status,
                ended_at=utc_now(),
                note=f"Process ended (exit={exit_code}).",
            )
            record = self.history_store.get(record_id)
            self.active_port_forwards.pop(record_id, None)
            if record is not None:
                self._log(
                    f"Port forward '{record.forward_name}' ended ({record.local_port} -> "
                    f"{record.instance_id}:{record.remote_port}, status={status})."
                )

    def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def _current_settings(self) -> tuple[str, str]:
        profile = self.query_one("#profile", Input).value.strip() or DEFAULT_PROFILE
        region = self.query_one("#region", Input).value.strip() or DEFAULT_REGION
        return profile, region

    def _selected_instance(self) -> InstanceSummary | None:
        table = self.query_one("#instance-table", DataTable)
        try:
            row = table.cursor_row
            if row < 0:
                raise IndexError
            return self.instances[row]
        except IndexError:
            return None

    def _render_instances(self) -> None:
        table = self.query_one("#instance-table", DataTable)
        table.clear(columns=False)
        for instance in self.instances:
            table.add_row(
                instance.display_name,
                instance.instance_id,
                instance.state,
                instance.instance_type,
                instance.private_ip or "-",
                instance.public_ip or "-",
                instance.availability_zone or "-",
            )
        if self.instances:
            table.move_cursor(row=0, column=0)
            self._update_command_preview_for_selection()
        else:
            self._set_command_preview("")

    def _set_status(self, message: str) -> None:
        try:
            self.query_one("#status", Static).update(message)
        except NoMatches:
            return

    def _show_command(self, command: list[str]) -> None:
        self._set_command_preview(shlex.join(command))

    def _set_command_preview(self, message: str) -> None:
        self.current_command = message.strip()
        try:
            self.query_one("#command-preview", Input).value = self.current_command
        except NoMatches:
            return

    def _update_command_preview_for_selection(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            self._set_command_preview("")
            return
        profile, region = self._current_settings()
        command = AwsInstance(
            instance_id=instance.instance_id,
            profile=profile,
            region=region,
        ).build_ssm_shell_command()
        self._show_command(command)

    def action_copy_command(self) -> None:
        if not self.current_command:
            self.notify("No command available to copy yet.", severity="warning")
            self._log("Copy command requested with no command available.")
            return
        self.copy_to_clipboard(self.current_command)
        self.notify("Command copied to clipboard.", severity="information")
        self._log("Copied command to clipboard.")

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#activity-log", Log).write_line(f"[{timestamp}] {message}")
        except NoMatches:
            return


def _parse_port(value: str) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if port < 1 or port > 65535:
        return None
    return port


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ").replace("+00:00", "Z")


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 3]}..."


def _name_from_preset_label(label: str) -> str:
    name, _, _ = label.partition(" (")
    return name.strip() or label.strip()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AWS EC2 Textual TUI")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="AWS CLI profile name")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region name")
    parser.add_argument(
        "--ports-config",
        default="port-forwards.yaml",
        help="YAML file for SSM port-forward defaults and presets",
    )
    parser.add_argument(
        "--history-file",
        default=str(DEFAULT_HISTORY_DB_PATH),
        help="SQLite file used to persist port-forward history",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    app = AwsTuiApp(
        profile=args.profile,
        region=args.region,
        ports_config=args.ports_config,
        history_file=args.history_file,
    )
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown_active_port_forwards(emit_ui=False)
        _restore_terminal_state()


def _restore_terminal_state() -> None:
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l\x1b[?25h")
        sys.stdout.flush()
    except OSError:
        pass
    if not sys.stdin.isatty():
        return
    try:
        subprocess.run(
            ["stty", "sane"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


if __name__ == "__main__":
    main()
