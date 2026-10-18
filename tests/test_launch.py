"""Tests for the launcher's Codex environment contract."""
import tomllib
import unittest
from pathlib import Path

from drover import brief, launch


def codex_settings(spec):
    argv, _ = launch.build_argv(spec)
    overrides = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c" and
                 argv[i + 1].startswith(("sandbox_workspace_write.", "shell_environment_policy."))]
    return tomllib.loads("\n".join(overrides))


class CodexWorkerEnvironmentTests(unittest.TestCase):
    def test_wsl_codex_worker_keeps_temp_policy_and_grants_its_scratch_root(self):
        scratch = "/tmp/drover-scratch/7-0123456789abcdef"
        settings = codex_settings({
            "backend": "codex", "model": "gpt-6.1-sol", "placement": "wsl", "cwd": "/home/acme/work",
            "system_append": "worker instructions", "user_prompt": "brief", "scratch_dir": scratch,
        })

        self.assertEqual(settings["sandbox_workspace_write"]["writable_roots"], [scratch])
        self.assertEqual(settings["shell_environment_policy"]["set"], {
            "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch,
        })

    def test_launcher_passes_worker_scratch_to_wsl_runner_only(self):
        launcher = launch.RunnerLauncher(runs_dir="/runs", distro="Ubuntu", drover_dir="/repo", windows=False)
        for placement, scratch, expected in (
                ("wsl", "/tmp/drover-scratch/7-0123456789abcdef", True),
                ("windows", r"R:\runs\7\scratch", False)):
            with self.subTest(placement=placement):
                spec = {
                    "run_id": 7, "nonce": "0123456789abcdef", "backend": "codex", "model": "gpt-6.1-sol",
                    "placement": placement, "cwd": "/home/acme/work" if placement == "wsl" else r"R:\work",
                    "system_append": "worker instructions", "user_prompt": "brief", "scratch_dir": scratch,
                }
                worker_argv, _ = launch.build_argv(spec)
                command, _ = launcher.command(spec, Path("/runs/7"), worker_argv)
                runner_args = command[:command.index("--")]

                self.assertEqual("--scratch-dir" in runner_args, expected)
                if expected:
                    self.assertEqual(runner_args[runner_args.index("--scratch-dir") + 1], scratch)

    def test_windows_codex_worker_keeps_existing_temp_setup_without_dotnet_overrides(self):
        scratch = r"R:\drover\runs\7\scratch"
        settings = codex_settings({
            "backend": "codex", "model": "gpt-6.1-sol", "placement": "windows", "cwd": r"R:\work",
            "system_append": "worker instructions", "user_prompt": "brief", "scratch_dir": scratch,
        })

        self.assertEqual(settings["sandbox_workspace_write"]["writable_roots"], [scratch])
        self.assertEqual(settings["shell_environment_policy"]["set"], {
            "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch,
        })

    def test_dotnet_brief_explains_offline_restore_and_cache_blockers(self):
        prompt = brief.assemble({
            "id": "T", "title": "Fix the build", "description": "Repair a .NET build",
            "scope_files": ["src/App.cs"], "size": "S",
        })["user_prompt"]
        guidance = prompt.lower()

        self.assertIn("network access is unavailable", guidance)
        self.assertIn("~/.nuget/packages", prompt)
        self.assertIn("package that is missing", guidance)
        self.assertIn("cache read is denied", guidance)
        self.assertIn("ask the owner to arrange an approved readable local cache", guidance)
