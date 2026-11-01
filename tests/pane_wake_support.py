"""What the pane_wake tests share: a role with mail, one row in a pane, and a clock that only moves on."""
import time

from drover import pane_wake
from drover.ledger import ops_panes
from drover.notifications import Notifications
from drover.pane_wake import TEXT, UNVERIFIED_S
from drover.panes import PaneService
from tests.pane_send_support import CLAUDE_IDLE, Case, PANE, composing
from tests.panes_support import ROLE

CLAUDE_WORKING = 'history\n───\n✻ Working (3s · esc to interrupt)\n❯ \n───\n⏵⏵ auto mode on\n'
CLAUDE_PAST_PROMPT = 'history\n❯ an earlier prompt\n\nits answer\n───\n❯ \n───\n⏵⏵ auto mode on\n'
QUEUE = f'Queued follow-up inputs\n  ↳ {TEXT}\n    shift+← edit last queued message\n'
QUEUED = f'• Working (3m • esc to interrupt)\n{QUEUE}› \n\n? for shortcuts\n'
QUEUED_IDLE = f'• Worked for 1m\n{QUEUE}› \n\n? for shortcuts\n'
WINDOW_S = 601
LATE = UNVERIFIED_S + 2      # past the limit whichever second the write was stamped in: stamps are whole seconds of the real clock


class Stage(Case):
    def setUp(self):
        super().setUp()
        self.time = time.time()     # writes are stamped with the real clock, so this one starts there and only moves on
        self.notifications = Notifications(self.conn, now=lambda: self.time)
        self.port.set_setting('dispatch_enabled', 'true')
        self.notifications.register(ROLE, allowed_models=['opus-5.5'], token_budget=100, wake_limit_per_hour=6,
                                    replacement_limit=1)
        self.notifications.issue_session(ROLE, 's1', ttl=86400)
        self.commits = 0
        self.run_ = dict(run_id=1, role_id=ROLE)

    def scene(self, screen=CLAUDE_IDLE, *, codex=False, **kw):
        row = dict(role_id=ROLE, run_id=1)
        self.stage(screen, row=row, **kw) if codex else self.windows(screen, row=row, **kw)
        self.service = PaneService(self.port, self.herdr, cfg={}, now=lambda: self.time)
        if codex:
            self.paste_settles_to(composing(TEXT))

    def mail(self):
        self.notifications.broadcast('owner', [ROLE], 'work', urgency='urgent')
        self.notifications.project()

    def deliver(self):
        pane_wake.deliver(self.service, self.run_)

    def woken(self, screen=CLAUDE_IDLE, **kw):
        """A wake sent, on the first tick it was due, with mail waiting."""
        self.scene(screen, **kw)
        self.mail()
        self.deliver()
        self.assertEqual(self.last()['state'], 'sent')
        self.fake.calls.clear()

    def show(self, screen, status='idle'):
        self.fake.screens[PANE] = screen
        self.fake.agents[PANE]['agent_status'] = status

    def commit(self, checkpoint=None):
        self.commits += 1
        lease = self.notifications.acquire(ROLE, 's1')
        messages = self.notifications.receive(ROLE)
        self.notifications.commit(ROLE, lease['lease_token'], checkpoint or {'processed': True},
                                  [m['delivery_id'] for m in messages], [], f'fixture-{self.commits}', release=True,
                                  delivery_versions={m['delivery_id']: m['message_version'] for m in messages})

    def wake_state(self):
        write = ops_panes.last_write(self.conn, self.row['id'], 'wake')
        return (write['state'], write['reason']) if write else None

    def budget(self):
        return self.notifications.role_state(ROLE)['wakes_reserved']

    def pending(self):
        return self.notifications.role_state(ROLE)['pending_wake']

    def notices(self):
        return [e['data']['notice'] for e in self.port.events(kind='pane.notice')]

    def open_notices(self):
        return self.conn.execute("SELECT COUNT(*) FROM questions WHERE status='open' AND title LIKE '%wake%'").fetchone()[0]

    def role_questions(self):
        return self.conn.execute("SELECT COUNT(*) FROM questions WHERE target_kind='director' AND status='open'").fetchone()[0]

    def stays_in_the_mailbox(self):
        self.assertEqual(self.sends(), [])
        self.assertIsNone(self.wake_state())
        self.assertEqual(self.budget(), 0)
        self.assertTrue(self.pending())
