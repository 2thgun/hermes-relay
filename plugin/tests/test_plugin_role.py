"""Role-aware plugin wiring tests (phase 6).

Every test uses fake subprocesses and a temporary ``HERMES_HOME``: no relay
listener is started, no device client is spawned, and no paired device or live
plugin tree is touched.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugin
from plugin.relay.device import supervisor
from plugin.relay.device.identity import ClientIdentity
from plugin.relay.device.settings import RoleSettings

REPO_ROOT = Path(plugin.__file__).resolve().parent.parent
DEFAULT_URL = "wss://relay.example:8767"


class FakeProcess:
    """Just enough of ``subprocess.Popen`` for the supervisor."""

    _next_pid = 43210

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakePopenFactory:
    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.instances: list[FakeProcess] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        process = FakeProcess(argv, **kwargs)
        self.instances.append(process)
        return process


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for name in (
        "HERMES_RELAY_ROLE",
        "HERMES_RELAY_URL",
        "HERMES_RELAY_CLIENT_STATE",
    ):
        monkeypatch.delenv(name, raising=False)
    supervisor._process = None
    yield hermes_home
    supervisor._process = None


@pytest.fixture
def fake_popen(monkeypatch):
    factory = FakePopenFactory()
    monkeypatch.setattr(
        supervisor,
        "subprocess",
        SimpleNamespace(
            Popen=factory,
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    return factory


@pytest.fixture
def embedded_starts(monkeypatch):
    calls: list[bool] = []

    def fake_start():
        calls.append(True)
        return object()

    monkeypatch.setattr("plugin.relay.embedded.start_embedded_relay", fake_start)
    return calls


def _state(home: Path) -> Path:
    return home / "relay" / "client.json"


def _write_pairing(
    state: Path,
    url: str = DEFAULT_URL,
    *,
    token: str = "session-token-for-tests",
    expires_at: float | None = None,
) -> None:
    identity = ClientIdentity.load(state)
    identity.remember_pairing(url, session_token=token, expires_at=expires_at)
    identity.save()


def _settings(
    home: Path,
    role: str,
    *,
    url: str | None = DEFAULT_URL,
    state: Path | None = None,
) -> RoleSettings:
    return RoleSettings(
        role=role,
        relay_url=url,
        client_state=state or _state(home),
    )


def _module_argv(argv: list[str]) -> list[str]:
    return argv[1:4]


def test_host_role_starts_only_the_relay(home, fake_popen, embedded_starts):
    summary = plugin.start_role_services(_settings(home, "host"))

    assert embedded_starts == [True]
    assert fake_popen.instances == []
    assert summary == {
        "role": "host",
        "host_started": True,
        "device_started": False,
        "device_reason": "not-client",
    }


def test_client_role_starts_only_the_device(home, fake_popen, embedded_starts):
    _write_pairing(_state(home))

    summary = plugin.start_role_services(_settings(home, "client"))

    assert embedded_starts == []
    assert len(fake_popen.calls) == 1
    assert summary["role"] == "client"
    assert summary["host_started"] is False
    assert summary["device_started"] is True
    assert summary["device_reason"] == "started"

    argv, kwargs = fake_popen.calls[0]
    layout = supervisor.detect_client_layout()
    assert layout.module == "plugin.relay.device"
    assert _module_argv(argv) == ["-m", "plugin.relay.device", "run"]
    assert kwargs["cwd"] == str(REPO_ROOT)
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["env"]["HERMES_RELAY_ROLE"] == "client"
    assert argv[argv.index("--url") + 1] == DEFAULT_URL
    assert argv[argv.index("--state") + 1] == str(_state(home))
    assert argv[argv.index("--cwd") + 1] == str(Path.home())

    pid_file = home / "relay" / "hermes-relay-device.pid"
    assert pid_file.exists()
    assert str(fake_popen.instances[0].pid) in pid_file.read_text(encoding="utf-8")


def test_client_role_without_token_refuses_to_spawn(
    home, fake_popen, embedded_starts, caplog
):
    settings = _settings(home, "client")

    with caplog.at_level(logging.WARNING, logger="plugin.relay.device.supervisor"):
        summary = plugin.start_role_services(settings)

    assert embedded_starts == []
    assert fake_popen.instances == []
    assert summary["device_started"] is False
    assert summary["device_reason"] == "no-token"
    assert "pair first" in caplog.text.lower()
    assert "hermes-relay-client pair" in caplog.text
    assert not (home / "relay" / "hermes-relay-device.pid").exists()


def test_client_role_without_url_refuses_to_spawn(
    home, fake_popen, embedded_starts, caplog
):
    settings = _settings(home, "client", url=None)

    with caplog.at_level(logging.WARNING, logger="plugin.relay.device.supervisor"):
        summary = plugin.start_role_services(settings)

    assert fake_popen.instances == []
    assert summary["device_reason"] == "no-url"
    assert "relay URL" in caplog.text


def test_client_role_accepts_url_from_active_pairing(home, fake_popen):
    _write_pairing(_state(home))

    summary = plugin.start_role_services(_settings(home, "client", url=None))

    assert summary["device_started"] is True
    argv = fake_popen.calls[0][0]
    assert "--url" not in argv  # `run` resolves the active pairing itself


def test_client_role_rejects_expired_token(home, fake_popen, caplog):
    _write_pairing(_state(home), expires_at=time.time() - 60)

    with caplog.at_level(logging.WARNING, logger="plugin.relay.device.supervisor"):
        summary = plugin.start_role_services(_settings(home, "client"))

    assert fake_popen.instances == []
    assert summary["device_reason"] == "token-expired"


def test_both_role_starts_relay_and_device(home, fake_popen, embedded_starts):
    _write_pairing(_state(home))

    summary = plugin.start_role_services(_settings(home, "both"))

    assert embedded_starts == [True]
    assert len(fake_popen.instances) == 1
    assert summary["host_started"] is True
    assert summary["device_started"] is True


def test_repeated_start_is_idempotent(home, fake_popen, embedded_starts):
    _write_pairing(_state(home))
    settings = _settings(home, "client")

    first = plugin.start_role_services(settings)
    second = plugin.start_role_services(settings)

    assert first["device_reason"] == "started"
    assert second["device_reason"] == "already-running"
    assert second["device_started"] is True
    assert len(fake_popen.instances) == 1


def test_stop_terminates_only_the_spawned_child(home, fake_popen):
    _write_pairing(_state(home))
    plugin.start_role_services(_settings(home, "client"))
    process = fake_popen.instances[0]

    supervisor.stop_device_client()

    assert process.terminated is True
    assert not (home / "relay" / "hermes-relay-device.pid").exists()


def test_repo_and_installed_layout_paths(tmp_path):
    repo = supervisor.detect_client_layout(tmp_path / "checkout" / "plugin")
    assert repo.module == "plugin.relay.device"
    assert repo.cwd == tmp_path / "checkout"
    assert repo.is_repo_layout is True

    installed = supervisor.detect_client_layout(tmp_path / "hermes-relay")
    assert installed.module == "relay.device"
    assert installed.cwd == tmp_path / "hermes-relay"
    assert installed.is_repo_layout is False

    settings = RoleSettings(role="client", relay_url=DEFAULT_URL)
    argv = supervisor.build_device_argv(settings, layout=installed)
    assert argv[1:4] == ("-m", "relay.device", "run")
    assert argv[argv.index("--state") + 1].endswith("client.json")

    # The real tree these tests run from is the repo layout.
    live = supervisor.detect_client_layout()
    assert live.module == "plugin.relay.device"
    assert live.cwd == REPO_ROOT


def test_service_command_uses_detected_module(tmp_path):
    from plugin.relay.device.service import build_client_command

    installed = supervisor.detect_client_layout(tmp_path / "hermes-relay")
    argv = build_client_command(
        python="/usr/bin/python3",
        url=DEFAULT_URL,
        state="/tmp/client.json",
        module=installed.module,
        workdir="/home/tester",
    )
    assert argv[:4] == ("/usr/bin/python3", "-m", "relay.device", "run")
    assert argv[argv.index("--url") + 1] == DEFAULT_URL
    assert argv[argv.index("--state") + 1] == "/tmp/client.json"
    assert argv[argv.index("--cwd") + 1] == "/home/tester"


def test_invalid_role_falls_back_to_host(home, fake_popen, embedded_starts, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_RELAY_ROLE", "server")

    with caplog.at_level(logging.WARNING):
        summary = plugin.start_role_services()

    assert summary["role"] == "host"
    assert summary["host_started"] is True
    assert fake_popen.instances == []
    assert "falling back to role=host" in caplog.text


class FakeCtx:
    """Minimal plugin context: records registrations, starts nothing."""

    def __init__(self):
        self.tools = {}
        self.cli = []
        self.commands = []
        self.hooks = []
        self.platforms = []
        self.sections = []

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_cli_command(self, **kwargs):
        self.cli.append(kwargs)

    def register_command(self, name, **kwargs):
        self.commands.append((name, kwargs))

    def register_hook(self, name, handler, **kwargs):
        self.hooks.append(name)

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_system_prompt_section(self, **kwargs):
        self.sections.append(kwargs)


def test_register_invokes_role_services_once(monkeypatch):
    calls = []

    def fake_role_services(settings=None):
        calls.append(settings)
        return {
            "role": "host",
            "host_started": False,
            "device_started": False,
            "device_reason": "not-client",
        }

    monkeypatch.setattr(plugin, "start_role_services", fake_role_services)
    ctx = FakeCtx()

    plugin.register(ctx)

    assert len(calls) == 1
    assert ctx.tools  # tool registration still happened
    assert {item["name"] for item in ctx.cli} == {"pair", "relay"}


def test_register_client_role_never_starts_the_host(
    home, fake_popen, embedded_starts, monkeypatch
):
    _write_pairing(_state(home))
    monkeypatch.setenv("HERMES_RELAY_ROLE", "client")
    monkeypatch.setenv("HERMES_RELAY_URL", DEFAULT_URL)
    ctx = FakeCtx()

    plugin.register(ctx)

    assert embedded_starts == []
    assert len(fake_popen.instances) == 1
    argv = fake_popen.calls[0][0]
    assert _module_argv(argv) == ["-m", "plugin.relay.device", "run"]


def test_legacy_media_client_import_still_wins():
    from plugin.relay import client as legacy_client
    from plugin.relay.client import register_media

    assert callable(register_media)
    assert register_media is legacy_client.register_media
    assert register_media.__module__ == "plugin.relay.client"

    # Adding the device supervisor must not create a shadowing
    # `plugin.relay.device.client` module.
    assert importlib.util.find_spec("plugin.relay.device.client") is None


def test_spawn_failure_is_reported_not_raised(home, monkeypatch, caplog):
    _write_pairing(_state(home))

    def broken_popen(argv, **kwargs):
        raise OSError("cannot exec")

    monkeypatch.setattr(
        supervisor,
        "subprocess",
        SimpleNamespace(
            Popen=broken_popen,
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    with caplog.at_level(logging.ERROR, logger="plugin.relay.device.supervisor"):
        summary = plugin.start_role_services(_settings(home, "client"))

    assert summary["device_started"] is False
    assert summary["device_reason"] == "spawn-failed"
    assert "Could not start" in caplog.text
