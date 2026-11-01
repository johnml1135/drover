"""Agent hook run inside WSL: reports the agent's lifecycle to Herdr so a pane shows its real state.

Herdr only sees wsl.exe, so a WSL agent must report itself. Outside Herdr this does nothing, and a
failed report never disturbs the agent. Stdlib only; runnable by path: python3 pane_status_hook.py --agent claude.
"""
import json
import os
import subprocess
import sys
import time

SOURCE = {'claude': 'herdr:claude-wsl', 'codex': 'herdr:codex-wsl'}
WSLENV_NAMES = ('HERDR_ENV', 'HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH', 'HERDR_WORKSPACE_ID', 'HERDR_TAB_ID')
TIMEOUT = 10
STATES = {
    'SessionStart': 'idle', 'Stop': 'idle', 'Interrupt': 'idle',
    'UserPromptSubmit': 'working', 'PreToolUse': 'working', 'PostToolUse': 'working', 'SubagentStart': 'working',
    'SubagentStop': 'working', 'PreCompact': 'working', 'PostCompact': 'working',
    'PermissionRequest': 'blocked', 'SessionEnd': 'release',
}


def state_of(event):
    return STATES.get(event)


def herdr_binary(path):
    """Resolve a Windows binary path for WSL; only ever called in the detached child."""
    if path.startswith('/'):
        return path
    done = subprocess.run(['wslpath', '-u', path], capture_output=True, text=True, timeout=TIMEOUT, check=True)
    return done.stdout.strip()


def commands(env, agent, event, session, seq):
    state = state_of(event)
    if state is None or env.get('HERDR_ENV') != '1' or not env.get('HERDR_PANE_ID') or not env.get('HERDR_BIN_PATH'):
        return []
    herdr, pane = env['HERDR_BIN_PATH'], env['HERDR_PANE_ID']
    common = ['--source', SOURCE[agent], '--agent', agent, '--seq', str(seq)]
    if state == 'release':
        return [[herdr, 'pane', 'release-agent', pane, *common]]
    out = []
    if event == 'SessionStart' and session:
        out.append([herdr, 'pane', 'report-agent-session', pane, *common, '--agent-session-id', session])
    out.append([herdr, 'pane', 'report-agent', pane, *common, '--state', state])
    return out


def run_commands(cmds):
    """Child side: resolve the binary once, run each command with a timeout; one failure never stops the rest."""
    try:
        herdr = herdr_binary(cmds[0][0])
    except Exception:
        return
    for cmd in cmds:
        try:
            subprocess.run([herdr, *cmd[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=TIMEOUT)
        except Exception:
            pass


def detached(cmds):
    """Start an isolated detached child that runs the commands in order, output discarded; never waited on."""
    flags = {'creationflags': 0x00000008 | 0x08000000} if os.name == 'nt' else {'start_new_session': True}
    subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--child', json.dumps(cmds)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, **flags)


def main(argv=None, *, stdin=None, env=None, run=None):
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if args[:1] == ['--child']:
            run_commands(json.loads(args[1]))
            return 0
        agent = args[args.index('--agent') + 1] if '--agent' in args else ''
        env = os.environ if env is None else env
        if agent not in SOURCE or env.get('HERDR_ENV') != '1':
            return 0
        try:
            data = json.load(sys.stdin if stdin is None else stdin)
        except Exception:
            return 0
        if not isinstance(data, dict):
            return 0
        session = data.get('session_id')
        cmds = commands(env, agent, data.get('hook_event_name'), session if isinstance(session, str) else None,
                        int(time.time() * 1000))
        if cmds:
            (run or detached)(cmds)
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
