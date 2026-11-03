"""Generate the per-platform schedule files (Task Scheduler XML or systemd units); the platform fork lives here, not in the standalone disk modules."""
import os
from pathlib import Path
import sys

from . import config, placement, process


def schedule_plan(cfg: dict, *, apply: bool = False) -> dict:
    """Generate only: pin interpreter, import root, machine home and shared state."""
    if not placement.IS_WINDOWS and os.environ.get('WSL_DISTRO_NAME'):
        raise ValueError('Install disk schedules using native Windows Python so the physical reader survives WSL shutdown')
    import datetime
    import getpass
    import xml.etree.ElementTree as ET
    files, commands = {}, []
    root = str(Path(__file__).resolve().parents[1])
    home, state = str(config.home().absolute()), str(config.state_dir(cfg).absolute())
    code = ('import os,runpy,sys;sys.path.insert(0,sys.argv[1]);'
            'os.environ["DROVER_HOME"]=sys.argv[2];'
            'os.environ["DROVER_PATHS_STATE_DIR"]=sys.argv[3];'
            'sys.argv=["drover",*sys.argv[4:]];runpy.run_module("drover",run_name="__main__")')
    mode = '--apply' if apply else '--dry-run'
    actions = [('DroverDiskTracker', 'drover-disk-tracker', ['disk-tracker', 'tick', mode]),
               ('DroverDiskCleanup', 'drover-disk-cleanup', ['disk-cleanup', '--mode', 'daily', mode])]
    for name, unit, args in actions:
        argv = [str(Path(sys.executable).absolute()), '-c', code, root, home, state, *args]
        if placement.IS_WINDOWS:
            ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
            ET.register_namespace('', ns)
            def node(parent, tag, text=None, **attributes):
                element = ET.SubElement(parent, '{' + ns + '}' + tag, attributes)
                element.text = text
                return element
            task = ET.Element('{' + ns + '}Task', version='1.4')
            triggers = node(task, 'Triggers')
            start = datetime.datetime.now().date().isoformat() + 'T03:00:00'
            if unit.endswith('tracker'):
                trigger = node(triggers, 'TimeTrigger')
                repetition = node(trigger, 'Repetition')
                node(repetition, 'Interval', 'PT15M')
                node(repetition, 'StopAtDurationEnd', 'false')
            else:
                trigger = node(triggers, 'CalendarTrigger')
            node(trigger, 'StartBoundary', start)
            node(trigger, 'Enabled', 'true')
            if not unit.endswith('tracker'):
                node(node(trigger, 'ScheduleByDay'), 'DaysInterval', '1')
            user = os.environ.get('USERNAME') or getpass.getuser()
            domain = os.environ.get('USERDOMAIN')
            user = domain + chr(92) + user if domain else user
            principal = node(node(task, 'Principals'), 'Principal', id='CurrentUser')
            node(principal, 'UserId', user)
            node(principal, 'LogonType', 'InteractiveToken')
            node(principal, 'RunLevel', 'LeastPrivilege')
            settings = node(task, 'Settings')
            for key, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('StartWhenAvailable', 'true'),
                               ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
                               ('ExecutionTimeLimit', 'PT0S')]:
                node(settings, key, value)
            action = node(node(task, 'Actions', Context='CurrentUser'), 'Exec')
            node(action, 'Command', argv[0])
            node(action, 'Arguments', process.list2cmdline(argv[1:]))
            node(action, 'WorkingDirectory', root)
            path = str(Path(state) / 'disk' / (name + '.xml'))
            files[path] = ET.tostring(task, encoding='unicode', xml_declaration=True)
            commands.append(['schtasks.exe', '/Create', '/TN', name, '/XML', path, '/F'])
        else:
            directory = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'systemd' / 'user'
            def quote(value):
                # systemd performs its own percent and dollar expansion, without a shell.
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n').replace('\r', '\\r') + '"'
            command = ' '.join(quote(value) for value in argv)
            files[str(directory / (unit + '.service'))] = (
                '[Unit]\nDescription=' + name + '\n[Service]\nType=oneshot\nExecStart=' + command + '\n')
            calendar = '*-*-* *:0/15:00' if unit.endswith('tracker') else '*-*-* 03:00:00'
            files[str(directory / (unit + '.timer'))] = (
                '[Unit]\nDescription=' + name + '\n[Timer]\nOnCalendar=' + calendar +
                '\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
    if not placement.IS_WINDOWS:
        commands = [['systemctl', '--user', 'daemon-reload'],
                    ['systemctl', '--user', 'enable', '--now', 'drover-disk-tracker.timer', 'drover-disk-cleanup.timer']]
    return dict(files=files, argv=commands)
