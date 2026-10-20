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

MIGRATIONS = [(1, V1), (2, V2), (3, V3), (4, V4), (5, V5), (6, V6), (7, V7)]
