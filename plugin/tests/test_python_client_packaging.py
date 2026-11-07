"""Phase-5 packaging tests for the headless Python relay client.

Covers the unified-artifact contract:

* importing the client with the server/plugin dependencies blocked,
* the ``hermes-relay-client`` entry point,
* state-store paths (default under ``$HERMES_HOME`` and an existing shared
  parent such as ``/tmp`` that must not be ``chmod``-ed),
* host/client/both role resolution,
* generated systemd user units and Windows Scheduled Task scripts.

No test starts a service, touches the live plugin, or needs a Windows or
Hyprland host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from plugin.relay.client.identity import (
    ClientIdentity,
    IdentityError,
    default_store_path,
)
from plugin.relay.client.installer import (
    apply_install_plan,
    build_install_plan,
)
from plugin.relay.client.service import (
    ServiceSpec,
    build_client_command,
    render_systemd_unit,
    render_windows_task_script,
)
from plugin.relay.client.settings import (
    ConfigError,
    load_plugin_settings,
    normalize_role,
    resolve_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

# Modules that the client must never need.  Blocking them in a subprocess is
# the offline stand-in for a minimal venv that only has aiohttp +
# websocket-client: if any import path regresses, the import raises.
BLOCKED_MODULES = (
    "requests",
    "yaml",
    "psutil",
    "httpx",
    "segno",
    "websocket",
    "cryptography",
    "PIL",
)

MINIMAL_IMPORT = r"""
import sys

BLOCKED = set(%(blocked)r)

from plugin.relay.client import __main__ as cli
assert callable(cli.main), "entry point main is not callable"
loaded = sorted({name.split(".")[0] for name in sys.modules} & BLOCKED)
assert not loaded, "heavy dependencies imported: %%s" %% loaded
assert "plugin.relay.server" not in sys.modules, "server module was imported"
print("minimal-import-ok")
"""

MINIMAL_CLI = r"""
import sys

BLOCKED = set(%(blocked)r)

from plugin.relay.client.__main__ import main
code = main(["tools", "--json"])
assert code == 0, code
loaded = sorted({name.split(".")[0] for name in sys.modules} & BLOCKED)
assert not loaded, "heavy dependencies imported: %%s" %% loaded
print("minimal-cli-ok")
"""

#: Import roots that a real client-only venv provides (aiohttp plus its
#: transitive dependencies).  Symlinking just these into a scratch directory
#: and running with ``python -S`` reproduces a minimal install without network
#: access and without the server/plugin packages on ``sys.path``.
CLIENT_DEPENDENCY_ROOTS = (
    "aiohttp",
    "aiohappyeyeballs",
    "aiosignal",
    "attr",
    "frozenlist",
    "multidict",
    "propcache",
    "yarl",
    "idna",
)


def build_client_dep_dir(target: Path) -> Path:
    import importlib.util

    deps = target / "client-deps"
    deps.mkdir()
    for name in CLIENT_DEPENDENCY_ROOTS:
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin:
            continue
        source = Path(spec.origin).parent
        os.symlink(source, deps / source.name, target_is_directory=True)
    return deps


def run_python(
    args: list[str],
    *,
    home: Path,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + existing_path if existing_path else ""
    )
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "LC_ALL": "C.UTF-8",
        }
    )
    for key in ("HERMES_RELAY_ROLE", "HERMES_RELAY_URL", "HERMES_RELAY_CLIENT_STATE"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [PYTHON, *args],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def run_minimal(
    args: list[str],
    *,
    deps: Path,
    home: Path,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run with only stdlib + aiohttp and its deps (``-S``, curated path)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "PYTHONPATH": f"{deps}{os.pathsep}{REPO_ROOT}",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "LC_ALL": "C.UTF-8",
    }
    return subprocess.run(
        [PYTHON, "-S", *args],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


class MinimalImportTests(unittest.TestCase):
    """The client must import with only its own runtime dependencies."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._deps_tmp = tempfile.TemporaryDirectory()
        cls.deps = build_client_dep_dir(Path(cls._deps_tmp.name))
        if not (cls.deps / "aiohttp").exists():
            cls._deps_tmp.cleanup()
            raise unittest.SkipTest("aiohttp is not importable in the test interpreter")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._deps_tmp.cleanup()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_client_imports_with_minimal_sys_path(self) -> None:
        result = run_minimal(
            ["-c", MINIMAL_IMPORT % {"blocked": BLOCKED_MODULES}],
            deps=self.deps,
            home=self.home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("minimal-import-ok", result.stdout)

    def test_client_cli_runs_with_minimal_sys_path(self) -> None:
        result = run_minimal(
            ["-c", MINIMAL_CLI % {"blocked": BLOCKED_MODULES}],
            deps=self.deps,
            home=self.home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("minimal-cli-ok", result.stdout)

    def test_bare_plugin_import_is_lazy(self) -> None:
        code = (
            "import sys, plugin\n"
            "heavy = sorted({'requests', 'yaml', 'psutil'} & set(sys.modules))\n"
            "assert not heavy, heavy\n"
            "print('plugin-lazy-ok')\n"
        )
        result = run_minimal(["-c", code], deps=self.deps, home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("plugin-lazy-ok", result.stdout)

    def test_relay_package_does_not_load_server(self) -> None:
        code = (
            "import sys, plugin.relay as relay\n"
            "assert relay.__version__\n"
            "assert 'plugin.relay.server' not in sys.modules\n"
            "print('relay-lazy-ok')\n"
        )
        result = run_minimal(["-c", code], deps=self.deps, home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("relay-lazy-ok", result.stdout)


class EntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_pyproject_declares_console_script(self) -> None:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = data["project"]["scripts"]
        self.assertEqual(
            scripts["hermes-relay-client"],
            "plugin.relay.client.__main__:main",
        )
        module_path, _, attribute = scripts["hermes-relay-client"].partition(":")
        module = __import__(module_path, fromlist=[attribute])
        self.assertTrue(callable(getattr(module, attribute)))

    def test_client_extra_lists_only_client_dependencies(self) -> None:
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        extra = " ".join(data["project"]["optional-dependencies"]["client"]).lower()
        self.assertIn("aiohttp", extra)
        self.assertIn("websocket-client", extra)
        self.assertNotIn("requests", extra)
        self.assertNotIn("pyyaml", extra)

    def test_module_entry_point_reports_version(self) -> None:
        result = run_python(["-m", "plugin.relay.client", "--version"], home=self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.1.0", result.stdout)

    def test_console_script_when_installed(self) -> None:
        executable = shutil.which("hermes-relay-client")
        if not executable:
            self.skipTest("hermes-relay-client is not installed in this environment")
        completed = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("hermes-relay-client", completed.stdout)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_default_store_uses_hermes_home(self) -> None:
        home = self.root / "custom-hermes"
        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            self.assertEqual(default_store_path(), home / "relay" / "client.json")
            identity = ClientIdentity.load()
            self.assertEqual(identity.path, home / "relay" / "client.json")

    def test_existing_parent_is_never_chmoded(self) -> None:
        """Regression for `--state /tmp/x.json` failing with EPERM."""
        parent = self.root / "shared"
        parent.mkdir()
        before = parent.stat().st_mode
        target = parent / "nested" / "deeper" / "client.json"

        calls: list[Path] = []
        real_chmod = os.chmod

        def guarded_chmod(path, mode, *args, **kwargs):
            resolved = Path(path)
            calls.append(resolved)
            if resolved == parent:
                raise PermissionError(1, "Operation not permitted", str(resolved))
            return real_chmod(path, mode, *args, **kwargs)

        identity = ClientIdentity.load(target)
        identity.remember_pairing("wss://relay.example:8767", session_token="tok")
        with patch("plugin.relay.client.identity.os.chmod", side_effect=guarded_chmod):
            saved = identity.save()

        self.assertEqual(saved, target)
        self.assertTrue(target.exists())
        self.assertNotIn(parent, calls)
        self.assertEqual(parent.stat().st_mode, before)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_state_file_under_shared_tmp_parent(self) -> None:
        target = Path(tempfile.gettempdir()) / f"hermes-relay-state-{os.getpid()}.json"
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        identity = ClientIdentity.load(target)
        identity.remember_pairing("wss://relay.example:8767", session_token="tok")
        saved = identity.save()
        self.assertEqual(saved, target)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["version"], 1)

    def test_unwritable_existing_directory_reports_identity_error(self) -> None:
        target = self.root / "locked" / "client.json"
        (self.root / "locked").mkdir()
        identity = ClientIdentity.load(target)
        identity.remember_pairing("wss://relay.example:8767", session_token="tok")
        with (
            patch("plugin.relay.client.identity.os.access", return_value=False),
            self.assertRaises(IdentityError) as raised,
        ):
            identity.save()
        self.assertIn("not writable", str(raised.exception))

    def test_cli_status_accepts_state_under_tmp(self) -> None:
        target = Path(tempfile.gettempdir()) / f"hermes-relay-cli-{os.getpid()}.json"
        with tempfile.TemporaryDirectory() as tmp:
            result = run_python(
                [
                    "-m",
                    "plugin.relay.client",
                    "status",
                    "--state",
                    str(target),
                    "--json",
                ],
                home=Path(tmp),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["store"], str(target))
        self.assertFalse(target.exists())

    def test_cli_state_flag_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            override = Path(tmp) / "override" / "state.json"
            result = run_python(
                [
                    "-m",
                    "plugin.relay.client",
                    "status",
                    "--state",
                    str(override),
                    "--json",
                ],
                home=home,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["store"], str(override))


class RoleSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "hermes"
        self.home.mkdir()

    def test_default_role_is_host(self) -> None:
        settings = resolve_settings(env={}, home=self.home, plugin_config={})
        self.assertEqual(settings.role, "host")
        self.assertTrue(settings.runs_host)
        self.assertFalse(settings.runs_client)
        self.assertEqual(settings.client_state, self.home / "relay" / "client.json")
        self.assertEqual(settings.sources["role"], "default")

    def test_env_sets_role_url_and_state(self) -> None:
        state = self.home / "custom" / "client.json"
        settings = resolve_settings(
            env={
                "HERMES_RELAY_ROLE": "client",
                "HERMES_RELAY_URL": "wss://env.example:8767",
                "HERMES_RELAY_CLIENT_STATE": str(state),
            },
            home=self.home,
            plugin_config={},
        )
        self.assertEqual(settings.role, "client")
        self.assertTrue(settings.runs_client)
        self.assertFalse(settings.runs_host)
        self.assertEqual(settings.relay_url, "wss://env.example:8767")
        self.assertEqual(settings.client_state, state)
        self.assertEqual(settings.sources["relay_url"], "env")

    def test_dotenv_is_read_when_process_env_is_empty(self) -> None:
        (self.home / ".env").write_text(
            "HERMES_RELAY_ROLE=both\nHERMES_RELAY_URL=wss://dotenv.example:8767\n",
            encoding="utf-8",
        )
        settings = resolve_settings(env={}, home=self.home, plugin_config={})
        self.assertEqual(settings.role, "both")
        self.assertTrue(settings.runs_host)
        self.assertTrue(settings.runs_client)
        self.assertEqual(settings.relay_url, "wss://dotenv.example:8767")
        self.assertEqual(settings.sources["role"], "dotenv")

    def test_plugin_config_mapping(self) -> None:
        settings = resolve_settings(
            env={},
            home=self.home,
            plugin_config={
                "role": "client",
                "relay_url": "wss://config.example:8767",
                "client_state": "~/relay/state.json",
            },
        )
        self.assertEqual(settings.role, "client")
        self.assertEqual(settings.relay_url, "wss://config.example:8767")
        self.assertEqual(settings.client_state, Path("~/relay/state.json").expanduser())
        self.assertEqual(settings.sources["client_state"], "config")

    def test_config_yaml_section(self) -> None:
        try:
            import yaml  # noqa: F401
        except ImportError:  # pragma: no cover - plugin tests always install PyYAML
            self.skipTest("PyYAML unavailable")
        (self.home / "config.yaml").write_text(
            "plugins:\n"
            "  entries:\n"
            "    hermes-relay:\n"
            "      settings:\n"
            "        role: client\n"
            "        relay_url: wss://yaml.example:8767\n"
            "        client_state: /var/lib/hermes/client.json\n",
            encoding="utf-8",
        )
        loaded = load_plugin_settings(self.home)
        self.assertEqual(loaded["role"], "client")
        settings = resolve_settings(env={}, home=self.home)
        self.assertEqual(settings.role, "client")
        self.assertEqual(settings.relay_url, "wss://yaml.example:8767")
        self.assertEqual(settings.client_state, Path("/var/lib/hermes/client.json"))

    def test_explicit_flags_win_over_env_and_config(self) -> None:
        settings = resolve_settings(
            role="host",
            relay_url="wss://cli.example:8767",
            client_state=self.home / "cli.json",
            env={
                "HERMES_RELAY_ROLE": "both",
                "HERMES_RELAY_URL": "wss://env.example:8767",
            },
            home=self.home,
            plugin_config={"role": "client", "relay_url": "wss://config.example:8767"},
        )
        self.assertEqual(settings.role, "host")
        self.assertEqual(settings.relay_url, "wss://cli.example:8767")
        self.assertEqual(settings.client_state, self.home / "cli.json")
        self.assertEqual(settings.sources["role"], "cli")

    def test_invalid_role_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            normalize_role("server")
        with self.assertRaises(ConfigError):
            resolve_settings(env={"HERMES_RELAY_ROLE": "server"}, home=self.home)

    def test_cli_status_reports_role_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_python(
                ["-m", "plugin.relay.client", "status", "--json"],
                home=Path(tmp),
                extra_env={
                    "HERMES_RELAY_ROLE": "both",
                    "HERMES_RELAY_URL": "wss://env.example:8767",
                },
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["role"], "both")
        self.assertEqual(payload["relay_url"], "wss://env.example:8767")

    def test_cli_run_uses_configured_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_python(
                ["-m", "plugin.relay.client", "run", "--json"],
                home=Path(tmp),
                extra_env={"HERMES_RELAY_URL": "ws://127.0.0.1:9"},
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertFalse(payload["ok"])
        self.assertIn("no stored session for ws://127.0.0.1:9", payload["error"])

    def test_cli_pair_without_url_fails_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_python(
                ["-m", "plugin.relay.client", "pair", "--code", "ABC123", "--json"],
                home=Path(tmp),
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertFalse(payload["ok"])
        self.assertIn("no relay URL supplied", payload["error"])


class InstallerGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _spec(
        self,
        *,
        url: str | None = "wss://relay.example:8767",
        state: str | None = "/home/tester/.hermes/relay/client.json",
    ) -> ServiceSpec:
        command = build_client_command(
            executable="/opt/hermes/bin/hermes-relay-client",
            url=url,
            state=state,
        )
        return ServiceSpec(command=command, working_directory="/home/tester")

    def test_systemd_unit_content(self) -> None:
        unit = render_systemd_unit(self._spec())
        self.assertIn("[Unit]", unit)
        self.assertIn("After=graphical-session.target", unit)
        self.assertIn("PartOf=graphical-session.target", unit)
        self.assertIn("WantedBy=graphical-session.target", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("Environment=HERMES_RELAY_ROLE=client", unit)
        self.assertIn("WorkingDirectory=/home/tester", unit)
        exec_line = next(
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        )
        self.assertIn("/opt/hermes/bin/hermes-relay-client run", exec_line)
        self.assertIn("--url wss://relay.example:8767", exec_line)
        self.assertIn("--state /home/tester/.hermes/relay/client.json", exec_line)

    def test_systemd_unit_quotes_paths_with_spaces(self) -> None:
        spec = ServiceSpec(
            command=("/opt/My Relay/bin/hermes-relay-client", "run"),
            working_directory="/home/tester",
        )
        unit = render_systemd_unit(spec)
        self.assertIn('"/opt/My Relay/bin/hermes-relay-client"', unit)

    def test_windows_task_script_content(self) -> None:
        spec = ServiceSpec(
            command=(
                r"C:\Python313\pythonw.exe",
                "-m",
                "plugin.relay.client",
                "run",
                "--url",
                "wss://relay.example:8767",
            ),
            working_directory=r"C:\Users\tester",
        )
        script = render_windows_task_script(spec)
        self.assertIn("$taskName = 'Hermes Relay Client'", script)
        self.assertIn(r"'C:\Python313\pythonw.exe'", script)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", script)
        self.assertIn("-Hidden", script)
        self.assertIn("RestartCount 999", script)
        self.assertIn("ExecutionTimeLimit ([TimeSpan]::Zero)", script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("wss://relay.example:8767", script)

    def test_linux_plan_is_side_effect_free(self) -> None:
        unit_dir = self.root / "units"
        plan = build_install_plan("linux", self._spec(), unit_dir=unit_dir)
        self.assertEqual(plan.platform, "linux")
        self.assertEqual(
            plan.artifacts[0].path,
            unit_dir / "systemd" / "user" / "hermes-relay-client.service",
        )
        self.assertEqual(plan.commands[0], ("systemctl", "--user", "daemon-reload"))
        self.assertEqual(
            plan.commands[1],
            ("systemctl", "--user", "enable", "--now", "hermes-relay-client.service"),
        )
        self.assertIn("would write:", plan.render())
        self.assertIn("would run: systemctl --user daemon-reload", plan.render())
        self.assertFalse(unit_dir.exists())

    def test_apply_plan_writes_and_calls_runner(self) -> None:
        unit_dir = self.root / "units"
        plan = build_install_plan("linux", self._spec(), unit_dir=unit_dir)
        calls: list[list[str]] = []

        class Completed:
            returncode = 0

        def runner(command, check=False):
            calls.append(list(command))
            return Completed()

        written = apply_install_plan(plan, runner=runner)
        self.assertEqual(written, [plan.artifacts[0].path])
        self.assertTrue(written[0].exists())
        self.assertIn("WantedBy=graphical-session.target", written[0].read_text())
        self.assertEqual(calls[0], ["systemctl", "--user", "daemon-reload"])
        self.assertEqual(calls[1][:3], ["systemctl", "--user", "enable"])

    def test_cli_print_matches_generator(self) -> None:
        result = run_python(
            [
                "-m",
                "plugin.relay.client",
                "install",
                "--platform",
                "linux",
                "--print",
                "--url",
                "wss://relay.example:8767",
                "--state",
                "/home/tester/client.json",
            ],
            home=self.root / "home",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[Unit]", result.stdout)
        self.assertIn("WantedBy=graphical-session.target", result.stdout)
        self.assertIn("--url wss://relay.example:8767", result.stdout)
        self.assertIn("--state /home/tester/client.json", result.stdout)
        self.assertNotIn("would write", result.stdout)

    def test_cli_dry_run_does_not_write(self) -> None:
        unit_dir = self.root / "units"
        result = run_python(
            [
                "-m",
                "plugin.relay.client",
                "install",
                "--platform",
                "linux",
                "--dry-run",
                "--unit-dir",
                str(unit_dir),
            ],
            home=self.root / "home",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would write:", result.stdout)
        self.assertIn("would run: systemctl --user enable --now", result.stdout)
        self.assertFalse(unit_dir.exists())

    def test_cli_windows_print(self) -> None:
        result = run_python(
            [
                "-m",
                "plugin.relay.client",
                "install",
                "--platform",
                "windows",
                "--print",
                "--url",
                "wss://relay.example:8767",
                "--python",
                r"C:\Python313\pythonw.exe",
            ],
            home=self.root / "home",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("$taskName = 'Hermes Relay Client'", result.stdout)
        self.assertIn(r"pythonw.exe", result.stdout)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", result.stdout)
        self.assertIn("-Hidden", result.stdout)

    def test_shell_wrapper_print(self) -> None:
        script = REPO_ROOT / "scripts" / "install-client-linux.sh"
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--print",
                "--url",
                "wss://relay.example:8767",
            ],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "PYTHON": PYTHON,
                "HOME": str(self.root / "home"),
                "HERMES_HOME": str(self.root / "home" / ".hermes"),
            },
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[Unit]", result.stdout)
        self.assertIn("WantedBy=graphical-session.target", result.stdout)
        self.assertIn("--url wss://relay.example:8767", result.stdout)

    def test_windows_wrapper_delegates_to_python_installer(self) -> None:
        script = (REPO_ROOT / "scripts" / "install-client-windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("install", script)
        self.assertIn('"--platform", "windows"', script)
        self.assertIn('"--print"', script)
        self.assertIn('"--dry-run"', script)
        self.assertIn("hermes-relay-client", script)


class ImportCompatibilityTests(unittest.TestCase):
    """Lazy re-exports keep the old attribute surface working."""

    def test_lazy_tool_tables_still_resolve(self) -> None:
        import plugin

        self.assertIn("_SCHEMAS", dir(plugin))
        schemas = plugin._SCHEMAS
        self.assertIn("android_ping", schemas)

    def test_relay_create_app_is_lazy_but_available(self) -> None:
        from plugin import relay

        self.assertTrue(callable(relay.create_app))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
