"""Exercise the fixed WSL probe with fake installs and no hub dependencies."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from drover.tool_discovery import DISCOVERY_SCRIPT, NODE_SHIM_REASON, parse_discovery_result


CLAUDE_SHIM_REASON = "node shim found; install a native Claude Linux binary to run without Node on PATH"


@unittest.skipUnless(os.name == "posix", "requires Bash and POSIX filesystem paths")
class ToolDiscovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.bash = shutil.which("bash")
        # Expose only probe utilities, so installed CLIs cannot affect the result.
        for tool in ("readlink", "sort", "od", "tr", "head"):
            (self.bin / tool).symlink_to(shutil.which(tool))

    def install(self, directory, *, node=False):
        launcher = directory / "claude"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        if node:
            shim = directory.parent / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            shim.parent.mkdir(parents=True, exist_ok=True)
            shim.write_text("#!/usr/bin/env node\nthrow new Error('must never run the shim');\n", encoding="utf-8")
            shim.chmod(0o755)
            launcher.symlink_to(shim)
        else:
            launcher.write_bytes(b"\x7fELFfake native binary")
            launcher.chmod(0o755)
        return str(launcher)

    def nvm_bin(self, version):
        return self.home / ".nvm" / "versions" / "node" / version / "bin"

    def probe(self):
        run = subprocess.run([self.bash, "-c", DISCOVERY_SCRIPT], capture_output=True, text=True, check=True,
                             env={"HOME": str(self.home), "PATH": str(self.bin)})
        return parse_discovery_result(run.stdout)

    def test_nvm_native_claude_uses_the_newest_node_version(self):
        self.install(self.nvm_bin("v9.11.2"))
        expected = self.install(self.nvm_bin("v22.4.1"))
        self.assertEqual(self.probe(), ({"claude": expected}, {}))

    def test_path_native_claude_takes_precedence_over_nvm(self):
        expected = self.install(self.bin)
        self.install(self.nvm_bin("v22.4.1"))
        self.assertEqual(self.probe(), ({"claude": expected}, {}))

    def test_node_shim_is_rejected_on_path_and_in_nvm_with_a_clear_reason(self):
        for directory in (self.bin, self.nvm_bin("v22.4.1")):
            with self.subTest(directory=directory):
                launcher = self.install(directory, node=True)
                try:
                    self.assertEqual(self.probe(), ({}, {"claude": CLAUDE_SHIM_REASON}))
                finally:
                    Path(launcher).unlink()

    def test_path_node_shim_does_not_hide_nvm_native_claude(self):
        self.install(self.bin, node=True)
        expected = self.install(self.nvm_bin("v22.4.1"))
        self.assertEqual(self.probe(), ({"claude": expected}, {}))

    def test_newer_node_shim_does_not_hide_older_nvm_native_claude(self):
        expected = self.install(self.nvm_bin("v20.1.0"))
        self.install(self.nvm_bin("v22.4.1"), node=True)
        self.assertEqual(self.probe(), ({"claude": expected}, {}))

    def test_newer_codex_directory_does_not_hide_older_nvm_native_codex(self):
        (self.nvm_bin("v22.4.1") / "codex").mkdir(parents=True, mode=0o755)
        native = self.nvm_bin("v20.1.0") / "codex"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"\x7fELFfake native binary")
        native.chmod(0o755)
        self.assertEqual(self.probe(), ({"codex": str(native)}, {}))

    def test_nvm_codex_shim_resolves_native_and_rejects_invalid_or_missing_native(self):
        node_bin = self.nvm_bin("v22.4.1")
        package = node_bin.parent / "lib" / "node_modules" / "@openai" / "codex"
        shim = package / "bin" / "codex.js"
        shim.parent.mkdir(parents=True)
        shim.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        shim.chmod(0o755)
        node_bin.mkdir()
        (node_bin / "codex").symlink_to(shim)
        native = package / "node_modules" / "@openai" / "codex-linux-x64" / "vendor" / \
            "x86_64-unknown-linux-musl" / "bin" / "codex"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"\x7fELFfake native binary")
        native.chmod(0o755)
        self.assertEqual(self.probe(), ({"codex": str(native)}, {}))

        native.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        self.assertEqual(self.probe(), ({}, {"codex": NODE_SHIM_REASON}))
        native.unlink()
        self.assertEqual(self.probe(), ({}, {"codex": NODE_SHIM_REASON}))
