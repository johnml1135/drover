"""Behavioral contract tests for the PowerShell CI test entrypoint."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.tiers import integration


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / '.drover' / 'scripts' / 'test.ps1'
RESULT_PREFIX = 'DROVER_ENTRYPOINT_RESULT:'


def _is_supported_platform():
    if os.name == 'nt':
        return True
    try:
        version = Path('/proc/version').read_text().lower()
    except OSError:
        version = ''
    return bool(os.environ.get('WSL_DISTRO_NAME')) or 'microsoft' in version


@integration
class CiEntrypointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which('pwsh')
        if cls.pwsh is None:
            if _is_supported_platform():
                raise AssertionError('PowerShell 7 (pwsh) is required on Windows and WSL for this contract test.')
            raise unittest.SkipTest('PowerShell 7 is unavailable outside the supported Windows and WSL platforms.')

    def _write_python_command(self, path, source):
        path.write_text(f'#!{sys.executable}\n' + source, encoding='utf-8')
        path.chmod(0o755)

    def _fake_tools(self, temp):
        bin_dir = temp / 'bin'
        bin_dir.mkdir()
        capture = temp / 'uv-calls.jsonl'
        fake_uv = '''import json, os, sys
record = {
    "argv": sys.argv[1:],
    "environment": {
        name: os.environ.get(name)
        for name in ("UV_PROJECT_ENVIRONMENT", "DROVER_TEST_JOBS", "PATH")
    },
}
with open(os.environ["DROVER_FAKE_UV_CAPTURE"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\\n")
sys.exit(int(os.environ.get("DROVER_FAKE_UV_EXIT", "0")))
'''
        fake_uv_path = temp / 'fake_uv.py'
        fake_uv_path.write_text(fake_uv, encoding='utf-8')
        if os.name == 'nt':
            uv = bin_dir / 'uv.cmd'
            uv.write_text(f'@echo off\r\n"{sys.executable}" "{fake_uv_path}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                          encoding='utf-8')
        else:
            self._write_python_command(bin_dir / 'uv', fake_uv)

            # Force the script through its Linux nvm resolution path so PATH
            # restoration is observable after the entrypoint returns.
            nvm_root = temp / 'nvm'
            nvm_root.mkdir()
            (nvm_root / 'nvm.sh').write_text('# fake nvm marker\n', encoding='utf-8')
            node_dir = temp / 'node' / 'bin'
            node_dir.mkdir(parents=True)
            node_path = node_dir / 'node'
            self._write_python_command(node_path, 'import sys\nsys.exit(0)\n')
            self._write_python_command(bin_dir / 'bash',
                                      'import os\nprint(os.environ["DROVER_FAKE_NODE_PATH"])\n')
        return bin_dir, capture

    def _run_entrypoint(self, kind='checks', changed_files='[]', uv_exit=0,
                        mixed_case_environment=False):
        with tempfile.TemporaryDirectory(prefix='drover-ci-entrypoint-') as temporary:
            temp = Path(temporary)
            bin_dir, capture = self._fake_tools(temp)
            original_path = str(bin_dir)
            if os.name == 'nt':
                original_path += os.pathsep + os.environ.get('PATH', '')

            env = os.environ.copy()
            env.update({
                'PATH': original_path,
                'UV_PROJECT_ENVIRONMENT': 'original-project-environment',
                'DROVER_FAKE_UV_CAPTURE': str(capture),
                'DROVER_FAKE_UV_EXIT': str(uv_exit),
            })
            env.pop('DROVER_TEST_JOBS', None)
            if kind is None:
                env.pop('DROVER_CI_KIND', None)
            else:
                env['DROVER_CI_KIND'] = kind
            if changed_files is None:
                env.pop('DROVER_CHANGED_FILES', None)
            else:
                env['DROVER_CHANGED_FILES'] = changed_files
            if os.name != 'nt':
                env['NVM_DIR'] = str(temp / 'nvm')
                env['DROVER_FAKE_NODE_PATH'] = str(temp / 'node' / 'bin' / 'node')

            escaped_entrypoint = "'" + str(ENTRYPOINT).replace("'", "''") + "'"
            harness = temp / 'invoke.ps1'
            harness.write_text(f'''$initializeMixedCaseEnvironment = ${str(mixed_case_environment).lower()}
if ($initializeMixedCaseEnvironment -and $IsWindows) {{
    $initialPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    Remove-Item -LiteralPath 'Env:UV_PROJECT_ENVIRONMENT' -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'Env:DROVER_TEST_JOBS' -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath 'Env:PATH' -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable('uV_pRoJeCt_EnViRoNmEnT', 'mixed-project-environment', 'Process')
    [Environment]::SetEnvironmentVariable('dRoVeR_tEsT_jObS', '17', 'Process')
    [Environment]::SetEnvironmentVariable('pAtH', $initialPath, 'Process')
}}
function Get-ProcessEnvironmentVariableName([string]$name) {{
    foreach ($key in [Environment]::GetEnvironmentVariables('Process').Keys) {{
        if ([string]::Equals([string]$key, $name, [StringComparison]::OrdinalIgnoreCase)) {{
            return [string]$key
        }}
    }}
    return $null
}}
$beforeProjectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$beforeTestJobs = [Environment]::GetEnvironmentVariable('DROVER_TEST_JOBS', 'Process')
$beforePath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
$beforeProjectEnvironmentName = Get-ProcessEnvironmentVariableName 'UV_PROJECT_ENVIRONMENT'
$beforeTestJobsName = Get-ProcessEnvironmentVariableName 'DROVER_TEST_JOBS'
$beforePathName = Get-ProcessEnvironmentVariableName 'PATH'
. {escaped_entrypoint}
$scriptExitCode = [int]$LASTEXITCODE
$afterProjectEnvironment = [Environment]::GetEnvironmentVariable('UV_PROJECT_ENVIRONMENT', 'Process')
$afterTestJobs = [Environment]::GetEnvironmentVariable('DROVER_TEST_JOBS', 'Process')
$afterPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
$afterProjectEnvironmentName = Get-ProcessEnvironmentVariableName 'UV_PROJECT_ENVIRONMENT'
$afterTestJobsName = Get-ProcessEnvironmentVariableName 'DROVER_TEST_JOBS'
$afterPathName = Get-ProcessEnvironmentVariableName 'PATH'
$state = [ordered]@{{
    exitCode = $scriptExitCode
    testJobsWasPresent = ($null -ne $beforeTestJobsName)
    pathAtStart = $beforePath
    variableNamesAtStart = [ordered]@{{
        UV_PROJECT_ENVIRONMENT = $beforeProjectEnvironmentName
        DROVER_TEST_JOBS = $beforeTestJobsName
        PATH = $beforePathName
    }}
    restored = (($null -ne $beforeProjectEnvironmentName) -eq ($null -ne $afterProjectEnvironmentName) -and
                ($null -eq $beforeProjectEnvironmentName -or $beforeProjectEnvironment -ceq $afterProjectEnvironment) -and
                ($null -ne $beforeTestJobsName) -eq ($null -ne $afterTestJobsName) -and
                ($null -eq $beforeTestJobsName -or $beforeTestJobs -ceq $afterTestJobs) -and
                ($null -ne $beforePathName) -eq ($null -ne $afterPathName) -and
                ($null -eq $beforePathName -or $beforePath -ceq $afterPath))
    testJobsPresent = ($null -ne $afterTestJobsName)
    environment = [ordered]@{{
        UV_PROJECT_ENVIRONMENT = $afterProjectEnvironment
        DROVER_TEST_JOBS = $afterTestJobs
        PATH = $afterPath
    }}
}}
[Console]::Out.WriteLine('{RESULT_PREFIX}' + (ConvertTo-Json -InputObject $state -Compress -Depth 3))
exit $scriptExitCode
''', encoding='utf-8')

            result = subprocess.run(
                [self.pwsh, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness)],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=45,
            )
            records = []
            if capture.exists():
                records = [json.loads(line) for line in capture.read_text(encoding='utf-8').splitlines()]
            state_line = next((line for line in result.stdout.splitlines() if line.startswith(RESULT_PREFIX)), None)
            state = json.loads(state_line[len(RESULT_PREFIX):]) if state_line else None
            return result, records, state

    @staticmethod
    def _runner_args(tier, modules=()):
        return ['run', '--project', 'hub', '--frozen', '--python', '3.13', 'python',
                'scripts/test.py', '--tier', tier, *modules]

    def test_checks_precheck_changed_test_modules_then_run_the_complete_default_tier(self):
        changed = json.dumps([
            'src/changed.py',
            'tests/test_ci_entrypoint.py',
            'tests/test_ci.py',
        ])
        result, calls, state = self._run_entrypoint(changed_files=changed)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['argv'] for call in calls], [
            self._runner_args('integration', ['tests.test_ci', 'tests.test_ci_entrypoint']),
            self._runner_args('default'),
        ])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]['environment']['UV_PROJECT_ENVIRONMENT'], 'original-project-environment')
        self.assertTrue(calls[0]['environment']['DROVER_TEST_JOBS'])
        self.assertIsNotNone(state)
        self.assertEqual(state['exitCode'], 0)
        self.assertTrue(state['restored'])
        self.assertFalse(state['testJobsWasPresent'])
        self.assertEqual(state['environment']['UV_PROJECT_ENVIRONMENT'], 'original-project-environment')
        self.assertFalse(state['testJobsPresent'])
        self.assertEqual(state['environment']['PATH'], state['pathAtStart'])

    def test_non_checks_kinds_run_both_complete_tiers(self):
        for kind in ('full', None, 'unrecognized'):
            with self.subTest(kind=kind):
                result, calls, state = self._run_entrypoint(kind=kind, changed_files='["tests/test_ci.py"]')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call['argv'] for call in calls], [
                    self._runner_args('integration'), self._runner_args('default')])
                self.assertTrue(state['restored'])

    def test_checks_without_changed_test_modules_run_the_complete_default_tier(self):
        result, calls, state = self._run_entrypoint(changed_files='["src/changed.py"]')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['argv'] for call in calls], [self._runner_args('default')])
        self.assertTrue(state['restored'])

    def test_missing_or_malformed_changed_file_metadata_runs_both_complete_tiers(self):
        for changed in (None, 'not-json', '["../tests/test_ci.py"]'):
            with self.subTest(changed=changed):
                result, calls, state = self._run_entrypoint(changed_files=changed)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call['argv'] for call in calls], [
                    self._runner_args('integration'), self._runner_args('default')])
                self.assertTrue(state['restored'])

    def test_changed_module_precheck_failure_propagates_and_short_circuits_default_tier(self):
        result, calls, state = self._run_entrypoint(changed_files='["tests/test_ci.py"]', uv_exit=23)

        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual([call['argv'] for call in calls], [
            self._runner_args('integration', ['tests.test_ci']),
        ])
        self.assertTrue(state['restored'])

    def test_mixed_case_environment_variables_are_restored_on_success_and_failure(self):
        for uv_exit in (0, 23):
            with self.subTest(uv_exit=uv_exit):
                result, calls, state = self._run_entrypoint(
                    changed_files='["tests/test_ci.py"]',
                    uv_exit=uv_exit,
                    mixed_case_environment=True,
                )

                self.assertEqual(result.returncode, uv_exit, result.stderr)
                expected_calls = [self._runner_args('integration', ['tests.test_ci'])]
                if uv_exit == 0:
                    expected_calls.append(self._runner_args('default'))
                self.assertEqual([call['argv'] for call in calls], expected_calls)
                self.assertIsNotNone(state)
                self.assertTrue(state['restored'])
                if os.name == 'nt':
                    for variable, expected_name in (
                        ('UV_PROJECT_ENVIRONMENT', 'uV_pRoJeCt_EnViRoNmEnT'),
                        ('DROVER_TEST_JOBS', 'dRoVeR_tEsT_jObS'),
                        ('PATH', 'pAtH'),
                    ):
                        self.assertEqual(state['variableNamesAtStart'][variable], expected_name)


if __name__ == '__main__':
    unittest.main()
