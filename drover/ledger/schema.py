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

MIGRATIONS = [(1, V1), (2, V2), (3, V3), (4, V4)]
