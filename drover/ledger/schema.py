"""Schema and forward-only migrations. Append to MIGRATIONS; never edit an applied entry."""

V1 = """
CREATE TABLE epics (
  id TEXT PRIMARY KEY, program TEXT NOT NULL, title TEXT NOT NULL, plan_md TEXT NOT NULL DEFAULT '',
  rank REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, epic_id TEXT REFERENCES epics(id), program TEXT NOT NULL, repo TEXT,
  title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
  acceptance TEXT NOT NULL DEFAULT '[]', scope_files TEXT NOT NULL DEFAULT '[]',
  priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0','P1','P2','P3','P4')),
  size TEXT NOT NULL DEFAULT 'M' CHECK (size IN ('S','M','L')),
  risk TEXT NOT NULL DEFAULT 'low' CHECK (risk IN ('low','high')),
  rank REAL NOT NULL, status TEXT NOT NULL, branch TEXT, worktree TEXT, owner_kind TEXT, model TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, policy_version TEXT,
  handoff TEXT NOT NULL DEFAULT '' CHECK (length(handoff) <= 1500), blocker TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX tasks_status ON tasks(status);
CREATE INDEX tasks_epic ON tasks(epic_id, rank);
CREATE TABLE task_deps (
  task_id TEXT NOT NULL REFERENCES tasks(id), depends_on TEXT NOT NULL REFERENCES tasks(id),
  kind TEXT NOT NULL DEFAULT 'blocks' CHECK (kind IN ('blocks','waits-for')),
  PRIMARY KEY (task_id, depends_on), CHECK (task_id <> depends_on));
CREATE INDEX task_deps_on ON task_deps(depends_on);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id), role TEXT NOT NULL,
  backend TEXT NOT NULL CHECK (backend IN ('claude','codex','local')), model TEXT, effort TEXT,
  policy_version TEXT, started TEXT NOT NULL, ended TEXT,
  tokens_in INTEGER, tokens_out INTEGER, tokens_cached INTEGER, allowance_used TEXT, wall_s REAL,
  outcome TEXT, gate_receipt TEXT, review_verdict TEXT, note TEXT CHECK (length(note) <= 300));
CREATE INDEX runs_task ON runs(task_id);
CREATE TABLE task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, epic_id TEXT, kind TEXT NOT NULL,
  actor TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL);
CREATE INDEX task_events_task ON task_events(task_id);
CREATE INDEX task_events_kind ON task_events(kind, at);
CREATE TRIGGER task_events_no_update BEFORE UPDATE ON task_events
  BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TRIGGER task_events_no_delete BEFORE DELETE ON task_events
  BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TABLE comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target_kind TEXT NOT NULL CHECK (target_kind IN ('epic','task')),
  target_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
  urgent INTEGER NOT NULL DEFAULT 0, replan INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
CREATE INDEX comments_target ON comments(target_kind, target_id);
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', options TEXT NOT NULL DEFAULT '[]',
  allow_text INTEGER NOT NULL DEFAULT 1, answer_choice TEXT, answer_text TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered')),
  asked TEXT NOT NULL, answered_at TEXT);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, question_name TEXT NOT NULL, criteria_version TEXT NOT NULL,
  model TEXT NOT NULL, state_hash TEXT NOT NULL, answer TEXT NOT NULL, probabilities TEXT, confidence REAL,
  shadow INTEGER NOT NULL DEFAULT 0, rule_answer TEXT, outcome TEXT, at TEXT NOT NULL);
CREATE INDEX decisions_q ON decisions(question_name, criteria_version);
CREATE TABLE policy_versions (version TEXT PRIMARY KEY, created TEXT NOT NULL, data TEXT NOT NULL, note TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

V2 = """
ALTER TABLE tasks ADD COLUMN approved_at TEXT;
UPDATE tasks SET approved_at = updated
  WHERE status IN ('ready','claimed','running','gated','reviewed','landed');
"""

V3 = """
ALTER TABLE tasks RENAME COLUMN repo TO repo_path;
ALTER TABLE tasks RENAME COLUMN program TO repo;
ALTER TABLE epics RENAME COLUMN program TO repo;
"""

V4 = """
ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'implementation'
  CHECK (kind IN ('research','investigation','implementation','fix','review','docs','mechanical'));
ALTER TABLE tasks ADD COLUMN "write" INTEGER NOT NULL DEFAULT 1 CHECK ("write" IN (0,1));
ALTER TABLE tasks ADD COLUMN read_access TEXT NOT NULL DEFAULT '[]';
UPDATE tasks SET read_access = json_array(repo);
CREATE TABLE task_links (
  task_id TEXT NOT NULL REFERENCES tasks(id), related_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, related_id), CHECK (task_id < related_id));
CREATE INDEX task_links_related ON task_links(related_id);
"""

V5 = """
ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
CREATE TABLE allowance_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, seat TEXT NOT NULL, ts REAL NOT NULL, window TEXT NOT NULL,
  used_pct REAL, resets_at REAL, raw_text TEXT NOT NULL, ok INTEGER NOT NULL);
CREATE INDEX allowance_seat_time ON allowance_readings(seat, ts, id);
CREATE INDEX allowance_seat_window ON allowance_readings(seat, window, ts, id);
CREATE TABLE run_stats (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE, data TEXT NOT NULL DEFAULT '{}');
INSERT INTO run_stats(run_id,data)
SELECT id, json_object('runs',1, 'sessions',CASE WHEN backend='local' THEN 0 ELSE 1 END,
  'handoffs',CASE WHEN outcome='rotated' THEN 1 ELSE 0 END, 'compactions',0,
  'retries',0, 'escalations',0, 'rework',0, 'seat',NULL, 'placement',NULL,
  'started',started, 'ended',ended, 'duration_s',wall_s, 'gate_result',gate_receipt, 'policy_version',policy_version,
  'peak_cpu_pct',NULL, 'peak_ram_mb',NULL, 'peak_disk_bytes',NULL,
  'tokens_by_model', json(CASE WHEN ended IS NULL THEN '{}' ELSE json_object(COALESCE(model,'unknown'), json_object(
    'tokens_in',COALESCE(tokens_in,0), 'tokens_out',COALESCE(tokens_out,0), 'tokens_cached',COALESCE(tokens_cached,0))) END))
FROM runs;
CREATE TABLE stats_hourly (ts TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE stats_daily (ts TEXT PRIMARY KEY, data TEXT NOT NULL);
"""

V6 = """

CREATE TABLE IF NOT EXISTS landing_lock (id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, tasks TEXT NOT NULL, outcome TEXT NOT NULL, tip TEXT,
  gates INTEGER NOT NULL, gate_s TEXT NOT NULL, red INTEGER NOT NULL, bisect_gates INTEGER NOT NULL,
  decisions INTEGER NOT NULL, dropped TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS base_gate (sha TEXT PRIMARY KEY, status TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_landed (
  task_id TEXT PRIMARY KEY, land_sha TEXT NOT NULL, tip TEXT NOT NULL, receipt TEXT, batch_id INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, rank INTEGER NOT NULL, holder TEXT NOT NULL, slot INTEGER, requested REAL NOT NULL, heartbeat REAL NOT NULL);
"""

V7 = """
ALTER TABLE tasks ADD COLUMN retry_after_run INTEGER NOT NULL DEFAULT 0 CHECK(retry_after_run >= 0);
ALTER TABLE tasks ADD COLUMN replans_used INTEGER NOT NULL DEFAULT 0 CHECK(replans_used >= 0);
ALTER TABLE tasks ADD COLUMN model_pin TEXT;
CREATE TABLE wait_subjects (
 subject_kind TEXT NOT NULL CHECK(subject_kind IN ('task','merge_queue')),
 subject_key TEXT NOT NULL, resume_state TEXT, reset_external INTEGER NOT NULL DEFAULT 0,
 parked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(subject_kind,subject_key));
CREATE TABLE wait_causes (
 cause TEXT PRIMARY KEY, question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL,
 handler TEXT NOT NULL CHECK(handler IN ('script','retry','choice','text','proposal','manual')),
 state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','authorized','attempting','resolved','closed')),
 answer TEXT, accepted_event INTEGER REFERENCES task_events(id), consumed_event INTEGER REFERENCES task_events(id),
 probe_session TEXT, probe_started REAL, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE waits (
 id INTEGER PRIMARY KEY, subject_kind TEXT NOT NULL, subject_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('task-terminal','owner-answer','base','replan','deadline','manual')),
 key TEXT NOT NULL, cause TEXT NOT NULL REFERENCES wait_causes(cause), timeout_at REAL,
 data TEXT NOT NULL DEFAULT '{}', created_event INTEGER NOT NULL REFERENCES task_events(id),
 released_event INTEGER REFERENCES task_events(id),
 FOREIGN KEY(subject_kind,subject_key) REFERENCES wait_subjects(subject_kind,subject_key) ON DELETE CASCADE);
CREATE UNIQUE INDEX waits_active ON waits(subject_kind,subject_key,kind,key,cause) WHERE released_event IS NULL;
CREATE INDEX waits_kind_key ON waits(kind,key) WHERE released_event IS NULL;
CREATE INDEX waits_cause ON waits(cause) WHERE released_event IS NULL;
CREATE INDEX waits_deadline ON waits(timeout_at) WHERE released_event IS NULL AND timeout_at IS NOT NULL;
CREATE TABLE replan_requests (
 id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 requesting_event INTEGER NOT NULL REFERENCES task_events(id), finished_event INTEGER REFERENCES task_events(id),
 current_run INTEGER REFERENCES runs(id) ON DELETE SET NULL,
 state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','owner','done')),
 disposition TEXT, snapshot TEXT NOT NULL DEFAULT '{}', result TEXT);
CREATE UNIQUE INDEX replan_unfinished ON replan_requests(task_id) WHERE state <> 'done';
ALTER TABLE base_gate RENAME TO base_gate_legacy;
CREATE TABLE base_gate (repo TEXT NOT NULL, sha TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('red','green')), at TEXT NOT NULL, PRIMARY KEY(repo,sha));
"""

V8 = """
CREATE TABLE replan_run_requests (
 run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 request_id INTEGER NOT NULL REFERENCES replan_requests(id) ON DELETE CASCADE,
 position INTEGER NOT NULL CHECK(position>=0),
 finished_event INTEGER REFERENCES task_events(id),
 PRIMARY KEY(run_id,request_id), UNIQUE(run_id,position));
CREATE INDEX replan_membership_request ON replan_run_requests(request_id,run_id);
INSERT INTO replan_run_requests(run_id,request_id,position,finished_event)
SELECT r.current_run,r.id,0,
 (SELECT e.id FROM task_events e WHERE e.kind='replan-attempt-finished'
  AND json_extract(e.data,'$.request')=r.id AND json_extract(e.data,'$.run')=r.current_run
  ORDER BY e.id DESC LIMIT 1)
FROM replan_requests r WHERE r.current_run IS NOT NULL;
INSERT OR IGNORE INTO replan_run_requests(run_id,request_id,position,finished_event)
SELECT run.id,r.id,0,
 (SELECT e.id FROM task_events e WHERE e.kind='replan-attempt-finished'
  AND json_extract(e.data,'$.request')=r.id AND json_extract(e.data,'$.run')=run.id
  ORDER BY e.id DESC LIMIT 1)
FROM task_events access JOIN runs run ON run.id=json_extract(access.data,'$.run')
 JOIN replan_requests r ON r.id=json_extract(access.data,'$.request_id')
WHERE access.kind='dispatch.access' AND run.role='planner' AND EXISTS (
 SELECT 1 FROM task_events cancel WHERE cancel.kind='launch-cancel'
 AND json_extract(cancel.data,'$.run')=run.id) AND NOT EXISTS (
 SELECT 1 FROM task_events cleared WHERE cleared.kind='launch-cancel-cleared'
 AND json_extract(cleared.data,'$.run')=run.id);
"""

V9 = """
CREATE TABLE director_roles (
 role_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
 generation INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'active',
 policy TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL, last_digest REAL NOT NULL);
CREATE TABLE task_ownership (
 task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, event_class TEXT NOT NULL DEFAULT '*',
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), revision INTEGER NOT NULL,
 PRIMARY KEY(task_id,event_class));
CREATE INDEX task_ownership_role ON task_ownership(role_id,task_id);
CREATE TABLE subscriptions (
 id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 revision INTEGER NOT NULL, created_position INTEGER NOT NULL);
CREATE TABLE subscription_versions (
 subscription_id TEXT NOT NULL REFERENCES subscriptions(id), revision INTEGER NOT NULL,
 after_event INTEGER NOT NULL, selectors TEXT NOT NULL, preferences TEXT NOT NULL,
 PRIMARY KEY(subscription_id,revision));
CREATE TABLE notification_intents (
 event_id INTEGER PRIMARY KEY REFERENCES task_events(id), schema_version INTEGER NOT NULL,
 event_class TEXT NOT NULL, role_id TEXT REFERENCES director_roles(role_id),
 ownership_revision INTEGER, subscription_id TEXT, subscription_revision INTEGER,
 preferences TEXT NOT NULL, envelope TEXT NOT NULL, actionable INTEGER NOT NULL DEFAULT 0,
 resolved INTEGER NOT NULL DEFAULT 0, projected INTEGER NOT NULL DEFAULT 0);
CREATE INDEX notification_intents_role ON notification_intents(role_id,event_id);
CREATE TABLE notification_inbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL UNIQUE REFERENCES task_events(id),
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), event_class TEXT NOT NULL,
 ownership_revision INTEGER NOT NULL, subscription_id TEXT, subscription_revision INTEGER,
 mode TEXT NOT NULL, actionable INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, issue_key TEXT,
 envelope TEXT NOT NULL, created REAL NOT NULL, offered_at REAL, acknowledged_at REAL,
 digest_id INTEGER);
CREATE UNIQUE INDEX notification_open_issue ON notification_inbox(issue_key) WHERE issue_key IS NOT NULL AND resolved=0;
CREATE INDEX notification_inbox_pending ON notification_inbox(role_id,acknowledged_at,id);
CREATE TABLE notification_digests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 created REAL NOT NULL, body TEXT NOT NULL, offered_at REAL, acknowledged_at REAL);
CREATE TABLE notification_coverage (
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), kind TEXT NOT NULL,
 count INTEGER NOT NULL DEFAULT 0, first_event INTEGER, last_event INTEGER, summarized_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(role_id,kind));
CREATE TABLE notification_compactions (
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), task_id TEXT NOT NULL,
 event_class TEXT NOT NULL, count INTEGER NOT NULL, first_event INTEGER NOT NULL,
 last_event INTEGER NOT NULL, summarized_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(role_id,task_id,event_class));
CREATE TABLE notification_positions (name TEXT PRIMARY KEY, event_id INTEGER NOT NULL);
INSERT INTO notification_positions(name,event_id) VALUES('inbox',0);
CREATE TABLE notification_escalations (
 role_id TEXT PRIMARY KEY REFERENCES director_roles(role_id), reason TEXT NOT NULL,
 created REAL NOT NULL, question_id INTEGER REFERENCES questions(id) ON DELETE SET NULL);
"""

V10 = """
CREATE TABLE director_sessions (
 credential_hash TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 session_id TEXT NOT NULL, generation INTEGER NOT NULL, expires REAL NOT NULL,
 capabilities TEXT NOT NULL);
CREATE INDEX director_sessions_role ON director_sessions(role_id,generation);
CREATE TABLE director_runtime (
 role_id TEXT PRIMARY KEY REFERENCES director_roles(role_id), session_id TEXT,
 checkpoint TEXT NOT NULL DEFAULT '{}', checkpoint_revision INTEGER NOT NULL DEFAULT 0,
 checkpoint_event INTEGER REFERENCES task_events(id), wake_position INTEGER NOT NULL DEFAULT 0,
 pending_wake INTEGER NOT NULL DEFAULT 0, replacements_used INTEGER NOT NULL DEFAULT 0,
 held_reason TEXT, fallback_question INTEGER REFERENCES questions(id));
CREATE TABLE director_leases (
 role_id TEXT PRIMARY KEY REFERENCES director_roles(role_id), session_id TEXT NOT NULL,
 generation INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL);
CREATE TABLE director_checkpoints (
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), revision INTEGER NOT NULL,
 event_id INTEGER NOT NULL UNIQUE REFERENCES task_events(id), checkpoint TEXT NOT NULL,
 PRIMARY KEY(role_id,revision));
CREATE TABLE director_processing (
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, event_id INTEGER NOT NULL UNIQUE REFERENCES task_events(id),
 receipt TEXT NOT NULL, generation INTEGER NOT NULL, session_id TEXT NOT NULL, lease_hash TEXT NOT NULL,
 PRIMARY KEY(role_id,request_key));
CREATE TABLE director_issue_leases (
 issue_id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 generation INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL,
 ownership_revision INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 attempt_limit INTEGER NOT NULL DEFAULT 2, fallback_question INTEGER REFERENCES questions(id),
 held_reason TEXT);
CREATE TABLE director_budget_entries (
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, created REAL NOT NULL, wakes INTEGER NOT NULL DEFAULT 0,
 tokens INTEGER NOT NULL DEFAULT 0, model TEXT, receipt TEXT NOT NULL,
 PRIMARY KEY(role_id,request_key));
CREATE INDEX director_budget_time ON director_budget_entries(role_id,created);
CREATE TABLE action_outbox (
 action_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES task_events(id),
 action_index INTEGER NOT NULL CHECK(action_index>=0), kind TEXT NOT NULL, payload TEXT NOT NULL,
 content_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','complete','held')), result TEXT,
 UNIQUE(event_id,action_index));
"""

V11 = """
ALTER TABLE director_leases ADD COLUMN control_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN parent_id TEXT REFERENCES tasks(id) ON DELETE SET NULL;
CREATE INDEX task_parent ON tasks(parent_id);
CREATE TABLE task_groups (
 group_id TEXT PRIMARY KEY, role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 parent_id TEXT REFERENCES tasks(id) ON DELETE SET NULL, mode TEXT NOT NULL CHECK(mode='synthesis'),
 revision INTEGER NOT NULL DEFAULT 1, sealed INTEGER NOT NULL DEFAULT 0,
 ready INTEGER NOT NULL DEFAULT 0, manifest TEXT, completion_event INTEGER UNIQUE REFERENCES task_events(id),
 failure_reason TEXT, issue_event INTEGER REFERENCES task_events(id));
CREATE TABLE task_group_members (
 group_id TEXT NOT NULL REFERENCES task_groups(group_id), child_id TEXT NOT NULL,
 lifecycle TEXT NOT NULL, PRIMARY KEY(group_id,child_id));
CREATE INDEX task_group_child ON task_group_members(child_id,group_id);
CREATE TABLE task_group_requests (
 group_id TEXT NOT NULL REFERENCES task_groups(group_id), request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(group_id,request_key));
CREATE TABLE notification_notices (
 notice_id TEXT PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE REFERENCES task_events(id),
 issuer TEXT NOT NULL, urgency TEXT NOT NULL, body TEXT NOT NULL,
 control_id TEXT, targets_all INTEGER NOT NULL DEFAULT 0);
CREATE TABLE notification_targets (
 event_id INTEGER NOT NULL REFERENCES task_events(id), role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 envelope TEXT NOT NULL, projected INTEGER NOT NULL DEFAULT 0, superseded INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(event_id,role_id));
CREATE INDEX notification_targets_role ON notification_targets(role_id,event_id);
ALTER TABLE notification_inbox RENAME TO notification_inbox_v10;
DROP INDEX notification_open_issue;
DROP INDEX notification_inbox_pending;
CREATE TABLE notification_inbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES task_events(id),
 role_id TEXT NOT NULL REFERENCES director_roles(role_id), event_class TEXT NOT NULL,
 ownership_revision INTEGER NOT NULL, subscription_id TEXT, subscription_revision INTEGER,
 mode TEXT NOT NULL, actionable INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, issue_key TEXT,
 envelope TEXT NOT NULL, created REAL NOT NULL, offered_at REAL, acknowledged_at REAL, digest_id INTEGER,
 UNIQUE(event_id,role_id));
INSERT INTO notification_inbox SELECT * FROM notification_inbox_v10;
UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM sqlite_sequence WHERE name='notification_inbox_v10'),0))
 WHERE name='notification_inbox';
INSERT INTO sqlite_sequence(name,seq)
 SELECT 'notification_inbox',seq FROM sqlite_sequence WHERE name='notification_inbox_v10'
 AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='notification_inbox');
DROP TABLE notification_inbox_v10;
CREATE UNIQUE INDEX notification_open_issue ON notification_inbox(issue_key) WHERE issue_key IS NOT NULL AND resolved=0;
CREATE INDEX notification_inbox_pending ON notification_inbox(role_id,acknowledged_at,id);
CREATE TABLE notification_control_state (
 id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL, stopped INTEGER NOT NULL);
INSERT INTO notification_control_state VALUES(1,0,0);
CREATE TABLE notification_controls (
 control_id TEXT PRIMARY KEY, event_id INTEGER UNIQUE REFERENCES task_events(id), kind TEXT NOT NULL,
 epoch INTEGER NOT NULL, skill TEXT, digest TEXT, active INTEGER NOT NULL DEFAULT 1,
 targets_all INTEGER NOT NULL DEFAULT 0);
CREATE TABLE notification_control_applications (
 control_id TEXT NOT NULL REFERENCES notification_controls(control_id), role_id TEXT NOT NULL REFERENCES director_roles(role_id),
 generation INTEGER NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL, digest TEXT, applied_at REAL NOT NULL,
 PRIMARY KEY(control_id,role_id,generation,session_id));
CREATE TABLE notification_control_requests (
 issuer TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, receipt TEXT NOT NULL,
 PRIMARY KEY(issuer,request_key));
"""

V12 = """
ALTER TABLE comments RENAME TO comments_v11;
DROP INDEX comments_target;
CREATE TABLE comments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 target_kind TEXT NOT NULL CHECK(target_kind IN ('epic','task','director','group')),
 target_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
 urgent INTEGER NOT NULL DEFAULT 0, replan INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
INSERT INTO comments SELECT * FROM comments_v11;
UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM sqlite_sequence WHERE name='comments_v11'),0))
 WHERE name='comments';
INSERT INTO sqlite_sequence(name,seq)
 SELECT 'comments',seq FROM sqlite_sequence WHERE name='comments_v11'
 AND NOT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name='comments');
DROP TABLE comments_v11;
CREATE INDEX comments_target ON comments(target_kind,target_id);
"""

MIGRATIONS = [(1, V1), (2, V2), (3, V3), (4, V4), (5, V5), (6, V6), (7, V7), (8, V8), (9, V9), (10, V10), (11, V11), (12, V12)]
