CREATE TABLE launch_options(
  copy_id INTEGER PRIMARY KEY REFERENCES copies(id) ON DELETE CASCADE,
  exe_relpath TEXT,
  args TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE play_sessions(
  id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  copy_id INTEGER REFERENCES copies(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK(kind IN ('tracked','manual')),
  state TEXT NOT NULL CHECK(state IN ('running','closed','recovered')),
  local_day TEXT NOT NULL,
  started_at TEXT NOT NULL,
  last_beat_at TEXT NOT NULL,
  ended_at TEXT,
  seconds INTEGER NOT NULL DEFAULT 0 CHECK(seconds >= 0),
  end_reason TEXT CHECK(end_reason IS NULL OR end_reason IN ('exited','panel_closed','recovered','manual')),
  edited INTEGER NOT NULL DEFAULT 0 CHECK(edited IN (0,1))
);
CREATE INDEX idx_play_sessions_work ON play_sessions(work_id);
CREATE UNIQUE INDEX idx_play_sessions_one_running ON play_sessions(state) WHERE state = 'running';
