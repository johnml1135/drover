"""Tests for the launcher's worker environment contracts."""
from drover import placement as placement_mod
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from drover import launch


def codex_settings(spec):
    argv, _ = launch.build_argv(spec)
    overrides = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c" and
                 argv[i + 1].startswith(("sandbox_workspace_write.", "shell_environment_policy."))]
    return tomllib.loads("\n".join(overrides))


def claude_settings(spec):
    argv, _ = launch.build_argv(spec)
    return argv, json.loads(argv[argv.index("--settings") + 1])


def claude_spec(**overrides):
    return {
        "backend": "claude", "model": "sonnet-5.5", "effort": "high", "placement": "wsl",
        "cwd": "/home/acme/work", "scratch_dir": "/tmp/drover-scratch/7-0123456789abcdef",
        "read_dirs": ["/home/acme/reads/snapshot"], "write": True, "system_append": "worker instructions",
        "user_prompt": "brief", **overrides,
    }


def runner_path(path, placement):
    return placement_mod.to_wsl_path(str(path)) if placement == "wsl" else placement_mod.to_windows_path(path)


class ClaudeWorkerAccessTests(unittest.TestCase):
    def test_claude_model_only_spec_defaults_to_read_only_current_directory(self):
        spec = claude_spec(write=False)
        del spec["cwd"]
        _, settings = claude_settings(spec)
        self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"][:2], ["/", runner_path(__import__("os").getcwd(), "wsl")])
        self.assertIn("Write", settings["permissions"]["deny"])

    def test_wsl_claude_write_worker_scopes_sandbox_to_worktree_and_scratch(self):
        argv, settings = claude_settings(claude_spec())
        sandbox = settings["sandbox"]

        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertTrue(sandbox["enabled"])
        self.assertTrue(sandbox["failIfUnavailable"])
        self.assertFalse(sandbox["allowUnsandboxedCommands"])
        self.assertTrue(sandbox["autoAllowBashIfSandboxed"])
        self.assertEqual(settings["env"], {
            "TMPDIR": "/tmp/drover-scratch/7-0123456789abcdef",
            "TMP": "/tmp/drover-scratch/7-0123456789abcdef",
            "TEMP": "/tmp/drover-scratch/7-0123456789abcdef",
        })
        self.assertEqual(sandbox["filesystem"]["allowWrite"], [
            "/home/acme/work", "/tmp/drover-scratch/7-0123456789abcdef",
        ])
        self.assertCountEqual(sandbox["filesystem"]["denyWrite"], [
            "/home/acme/reads/snapshot", "/tmp/claude", "/private/tmp/claude", "~/.npm/_logs", "~/.claude/debug",
        ])
        self.assertEqual(settings["permissions"]["deny"], ["Edit(//home/acme/reads/snapshot/**)"])
        self.assertEqual(settings["permissions"]["allow"], [
            "Read", "Grep", "Glob", "Edit(//tmp/drover-scratch/7-0123456789abcdef/**)",
        ])

    def test_wsl_claude_review_and_read_only_workers_cannot_edit(self):
        base = claude_spec()
        for spec in (claude_spec(role="review"), claude_spec(write=False), claude_spec(write=None),
                     {key: value for key, value in base.items() if key != "write"}):
            with self.subTest(role=spec.get("role"), write=spec.get("write")):
                argv, settings = claude_settings(spec)
                sandbox = settings["sandbox"]
                tools = argv[argv.index("--tools") + 1]

                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                self.assertEqual(tools.split(","), ["Read", "Grep", "Glob", "Bash"])
                self.assertTrue(sandbox["enabled"])
                self.assertTrue(sandbox["failIfUnavailable"])
                self.assertFalse(sandbox["allowUnsandboxedCommands"])
                self.assertTrue(sandbox["autoAllowBashIfSandboxed"])
                self.assertEqual(sandbox["filesystem"]["allowWrite"], ["/tmp/drover-scratch/7-0123456789abcdef"])
                deny = sandbox["filesystem"]["denyWrite"]
                self.assertEqual(deny[:2], ["/", "/home/acme/work"])  # "/" alone leaves cwd writable
                self.assertIn("/home/acme/reads/snapshot", deny)
                self.assertEqual(settings["permissions"]["allow"], ["Read", "Grep", "Glob"])
                self.assertCountEqual(settings["permissions"]["deny"], [
                    "Edit", "Write", "NotebookEdit", "Edit(//home/acme/reads/snapshot/**)",
                ])
                self.assertNotIn("--add-dir", argv)

    def test_windows_claude_write_workers_use_permissions_with_job_object_not_sandbox(self):
        spec = claude_spec(
            placement="windows", cwd=r"R:\work", scratch_dir=r"R:\runs\7\scratch",
            read_dirs=[r"R:\reads\snapshot"],
        )
        default_placement = {key: value for key, value in spec.items() if key != "placement"}
        for candidate in (spec, default_placement):
            with self.subTest(placement=candidate.get("placement")):
                argv, settings = claude_settings(candidate)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
                self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
                self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
                self.assertEqual(settings["env"], dict.fromkeys(("TMPDIR", "TMP", "TEMP"), spec["scratch_dir"]))
                self.assertEqual(settings["permissions"]["allow"], [
                    "Read", "Grep", "Glob", "Edit(//R:/runs/7/scratch/**)", "Bash",
                ])
                self.assertEqual(settings["permissions"]["deny"], ["Edit(//R:/reads/snapshot/**)"])
                self.assertNotIn("sandbox", settings)
                self.assertNotIn("--add-dir", argv)

    def test_windows_claude_write_worker_reaches_runner_with_private_scratch(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(launch.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = '{"runner_pid":1234}'
                launcher = launch.RunnerLauncher(runs_dir=temp, run=run, windows=True,
                                                 which=lambda name: r"R:\bin\claude.exe")
                handle = launcher.launch(claude_spec(placement="windows", cwd=r"R:\work", read_dirs=[], run_id=7))
                self.addCleanup(handle.cancel)
                run.assert_called_once()
                command = run.call_args.args[0]
                worker = command[command.index("--") + 1:]
                settings = json.loads(worker[worker.index("--settings") + 1])
                self.assertEqual(worker[worker.index("--permission-mode") + 1], "acceptEdits")
                scratch = next((Path(temp) / "7").glob("scratch-*"))
                for name in ("TMPDIR", "TMP", "TEMP"):
                    self.assertEqual(settings["env"][name], placement_mod.to_windows_path(scratch))
                    self.assertEqual(run.call_args.kwargs["env"][name], str(scratch))
                self.assertIn(launch._claude_edit_rule(placement_mod.to_windows_path(scratch)),
                              settings["permissions"]["allow"])

    def test_windows_claude_review_and_read_only_workers_have_no_shell_or_sandbox(self):
        base = claude_spec(placement="windows", cwd=r"R:\work", scratch_dir=r"R:\runs\7\scratch",
                          read_dirs=[r"R:\reads\snapshot"])
        for spec in ({**base, "role": "review"}, {**base, "write": False}, {**base, "write": None},
                     {key: value for key, value in base.items() if key != "write"}):
            with self.subTest(role=spec.get("role"), write=spec.get("write")):
                argv, settings = claude_settings(spec)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
                self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
                self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
                self.assertEqual(settings["permissions"]["allow"], ["Read", "Grep", "Glob"])
                for tool in ("Edit", "Write", "NotebookEdit", "Bash"):
                    self.assertIn(tool, settings["permissions"]["deny"])
                self.assertIn("Edit(//R:/reads/snapshot/**)", settings["permissions"]["deny"])
                self.assertEqual(settings["env"], dict.fromkeys(("TMPDIR", "TMP", "TEMP"), base["scratch_dir"]))
                self.assertNotIn("sandbox", settings)
                self.assertNotIn("--add-dir", argv)

    def test_claude_model_only_spec_defaults_to_windows_read_only_without_sandbox(self):
        spec = claude_spec()
        for key in ("placement", "write", "cwd", "scratch_dir", "read_dirs"):
            del spec[key]
        argv, settings = claude_settings(spec)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertNotIn("sandbox", settings)

    def test_claude_writer_denies_implicit_external_git_metadata_grants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cwd = root / "work tree"
            cwd.mkdir()
            common = root / "main" / ".git"
            gitdir = common / "worktrees" / "task"
            gitdir.mkdir(parents=True)
            for absolute in (False, True):
                (cwd / ".git").write_text(
                    f"gitdir: {gitdir if absolute else '../main/.git/worktrees/task'}\n", encoding="utf-8")
                (gitdir / "commondir").write_text(f"{common if absolute else '../..'}\n", encoding="utf-8")
                for placement in ("wsl", "windows"):
                    with self.subTest(absolute=absolute, placement=placement):
                        _, settings = claude_settings(claude_spec(cwd=str(cwd), placement=placement))
                        denied = runner_path(common, placement)
                        self.assertIn(launch._claude_edit_rule(denied), settings["permissions"]["deny"])
                        if placement == "wsl":
                            self.assertIn(denied, settings["sandbox"]["filesystem"]["denyWrite"])
                            self.assertNotIn(denied, settings["sandbox"]["filesystem"]["allowWrite"])

    def test_claude_writer_denies_separate_git_directory_without_commondir(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve() / "work"
            cwd.mkdir()
            gitdir = cwd.parent / "metadata"
            gitdir.mkdir()
            (cwd / ".git").write_text("gitdir: ../metadata\n", encoding="utf-8")
            _, settings = claude_settings(claude_spec(cwd=str(cwd)))
            self.assertIn(runner_path(gitdir, "wsl"), settings["sandbox"]["filesystem"]["denyWrite"])

    def test_claude_writer_does_not_deny_git_metadata_inside_its_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve()
            (cwd / ".git").mkdir()
            _, settings = claude_settings(claude_spec(cwd=str(cwd)))
            self.assertNotIn(runner_path(cwd / ".git", "wsl"), settings["sandbox"]["filesystem"]["denyWrite"])

    def test_claude_writer_resolves_external_git_metadata_through_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve() / "work"
            cwd.mkdir()
            gitdir = cwd.parent / "metadata"
            gitdir.mkdir()
            try:
                (cwd / ".git").symlink_to(gitdir, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            _, settings = claude_settings(claude_spec(cwd=str(cwd)))
            self.assertIn(runner_path(gitdir, "wsl"), settings["sandbox"]["filesystem"]["denyWrite"])

    def test_claude_writer_fails_closed_when_git_metadata_cannot_be_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve()
            for content in ("not a gitdir\n", "gitdir: \n", "gitdir: missing\n"):
                with self.subTest(content=content):
                    (cwd / ".git").write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "git metadata"):
                        claude_settings(claude_spec(cwd=str(cwd)))
            (cwd / ".git").unlink()
            (cwd / ".git").mkdir()
            for target in ("\n", "missing\n"):
                with self.subTest(commondir=target):
                    (cwd / ".git" / "commondir").write_text(target, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "git metadata"):
                        claude_settings(claude_spec(cwd=str(cwd)))

    def test_claude_writer_fails_closed_when_git_metadata_is_unreadable(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve()
            (cwd / ".git").write_text("gitdir: ../metadata\n", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValueError, "git metadata"):
                    claude_settings(claude_spec(cwd=str(cwd)))
            with patch.object(Path, "lstat", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ValueError, "git metadata"):
                    claude_settings(claude_spec(cwd=str(cwd)))

    def test_claude_worker_translates_windows_paths_for_wsl(self):
        argv, settings = claude_settings(claude_spec(
            cwd=r"R:\work", scratch_dir=r"R:\runs\7\scratch", read_dirs=[r"R:\reads\snapshot"],
        ))

        self.assertEqual(settings["sandbox"]["filesystem"]["allowWrite"], ["/mnt/r/work", "/mnt/r/runs/7/scratch"])
        self.assertEqual(settings["env"]["TMPDIR"], "/mnt/r/runs/7/scratch")
        self.assertIn("/mnt/r/reads/snapshot", settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertEqual(settings["permissions"]["deny"], ["Edit(//mnt/r/reads/snapshot/**)"])

    def test_claude_write_roots_cannot_overlap_read_snapshots(self):
        for placement, cwd, scratch, reads in (
                ("wsl", "/home/acme/work", "/tmp/task", ["/home/acme/work/reads"]),
                ("wsl", "/home/acme/work", "/tmp/task", ["/tmp/task/reads"]),
                ("wsl", r"R:\work", r"R:\scratch", [r"R:\work\reads"]),
                ("wsl", r"R:\work", r"R:\scratch", [r"R:\scratch\reads"]),
                ("windows", r"R:\work", r"R:\scratch", [r"R:\work\reads"]),
                ("windows", r"R:\work", r"R:\scratch", [r"R:\scratch\reads"])):
            with self.subTest(placement=placement, reads=reads):
                with self.assertRaisesRegex(ValueError, "read-only checkout"):
                    launch.build_argv(claude_spec(placement=placement, cwd=cwd, scratch_dir=scratch, read_dirs=reads))

    def test_runner_passes_claude_scratch_environment_to_wsl_and_windows_workers(self):
        for placement, cwd, scratch in (
                ("wsl", "/home/acme/work", "/tmp/drover-scratch/7-0123456789abcdef"),
                ("windows", r"R:\work", r"R:\runs\7\scratch")):
            with self.subTest(placement=placement):
                spec = claude_spec(placement=placement, cwd=cwd, scratch_dir=scratch,
                                   run_id=7, nonce="0123456789abcdef")
                argv, _ = launch.build_argv(spec)
                launcher = launch.RunnerLauncher(
                    runs_dir="/runs", distro="Ubuntu", drover_dir="/repo", python=r"R:\python.exe",
                    windows=placement == "windows", which=lambda name: r"R:\bin\claude.exe",
                )
                command, env = launcher.command(spec, Path("/runs/7"), argv)

                if placement == "wsl":
                    self.assertEqual(command[command.index("--scratch-dir") + 1], scratch)
                else:
                    self.assertEqual((env["TMPDIR"], env["TMP"], env["TEMP"]), (scratch, scratch, scratch))

    def test_claude_worker_argv_never_enables_permission_bypass(self):
        for placement in ("wsl", "windows"):
            specs = [claude_spec(placement=placement), claude_spec(placement=placement, role="review"),
                     claude_spec(placement=placement, write=False)]
            for spec in specs:
                with self.subTest(placement=placement, role=spec.get("role"), write=spec.get("write")):
                    argv, settings = claude_settings(spec)
                    self.assertNotIn("--dangerously-skip-permissions", argv)
                    self.assertNotIn("--allow-dangerously-skip-permissions", argv)
                    self.assertNotIn("bypassPermissions", " ".join(argv))
                    self.assertNotIn("bypassPermissions", json.dumps(settings))

    def test_supplied_argv_cannot_enable_claude_permission_bypass(self):
        for args in (
                ["--dangerously-skip-permissions"], ["--allow-dangerously-skip-permissions"],
                ["--permission-mode", "bypassPermissions"], ["--permission-mode=bypassPermissions"],
                ["--dangerously-skip-permissions=true"], ["--allow-dangerously-skip-permissions=true"],
                ["--settings", '{"permissions":{"defaultMode":"bypassPermissions"}}'],
                ['--settings={"permissions":{"defaultMode":"bypassPermissions"}}']):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "permission bypass"):
                launch.build_argv({"argv": ["claude", *args], "user_prompt": "brief"})


class CodexWorkerEnvironmentTests(unittest.TestCase):
    def test_wsl_codex_worker_keeps_temp_policy_and_grants_its_scratch_root(self):
        scratch = "/tmp/drover-scratch/7-0123456789abcdef"
        settings = codex_settings({
            "backend": "codex", "model": "gpt-6.1-sol", "placement": "wsl", "cwd": "/home/acme/work",
            "system_append": "worker instructions", "user_prompt": "brief", "scratch_dir": scratch,
        })

        self.assertEqual(settings["sandbox_workspace_write"]["writable_roots"], [scratch])
        self.assertIs(settings["sandbox_workspace_write"]["exclude_slash_tmp"], True)
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

    def test_windows_codex_worker_keeps_temp_setup(self):
        scratch = r"R:\drover\runs\7\scratch"
        settings = codex_settings({
            "backend": "codex", "model": "gpt-6.1-sol", "placement": "windows", "cwd": r"R:\work",
            "system_append": "worker instructions", "user_prompt": "brief", "scratch_dir": scratch,
        })

        self.assertEqual(settings["sandbox_workspace_write"]["writable_roots"], [scratch])
        self.assertEqual(settings["shell_environment_policy"]["set"], {
            "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch,
        })


class ConfiguredWritableRoots(unittest.TestCase):
    def test_extra_root_cannot_cover_a_read_checkout(self):
        spec = {'backend': 'codex', 'model': 'gpt-6.1-sol', 'placement': 'wsl', 'cwd': '/home/acme/work',
                'system_append': 'instructions', 'user_prompt': 'brief', 'write': True,
                'read_dirs': ['/tmp/reads/dependency'],
                'run_config': {'sandbox_writable_roots': ['/tmp/reads']}}
        with self.assertRaises(ValueError):
            launch.build_argv(spec)
