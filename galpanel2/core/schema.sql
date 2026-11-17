CREATE TABLE library_info(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 必有键：library_uuid（uuid4 字符串）、created_at、app_version
CREATE TABLE sources(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,
  path_key TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL,
  last_scan_at TEXT
);
CREATE TABLE works(
  id INTEGER PRIMARY KEY,
  uuid TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL CHECK(length(trim(title))>0),
  developer TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0,1)),
  added_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE copies(
  id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK(kind IN ('folder','archive')),
  path TEXT NOT NULL,
  path_key TEXT NOT NULL UNIQUE,
  exe_relpath TEXT,
  size_bytes INTEGER,
  availability TEXT NOT NULL DEFAULT 'unknown'
    CHECK(availability IN ('unknown','ok','missing','error')),
  checked_at TEXT,
  origin TEXT NOT NULL CHECK(origin IN ('scan','legacy')),
  added_at TEXT NOT NULL
);
CREATE INDEX idx_copies_work ON copies(work_id);
CREATE INDEX idx_copies_source ON copies(source_id);
CREATE TABLE covers(
  work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('legacy','user'))
);
CREATE TABLE imports(
  id INTEGER PRIMARY KEY,
  source_db_path TEXT NOT NULL,
  sha256_before TEXT NOT NULL,
  sha256_after TEXT NOT NULL,
  columns_json TEXT NOT NULL,
  imported INTEGER NOT NULL,
  rejected_json TEXT NOT NULL,
  imported_at TEXT NOT NULL
);
CREATE TABLE legacy_fields(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  import_id INTEGER NOT NULL REFERENCES imports(id),
  column_name TEXT NOT NULL,
  value_json TEXT NOT NULL,
  PRIMARY KEY(work_id, column_name)
);
CREATE TABLE legacy_play_logs(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  day TEXT NOT NULL,
  minutes INTEGER NOT NULL,
  PRIMARY KEY(work_id, day)
);
CREATE TABLE legacy_sources(
  import_id INTEGER NOT NULL REFERENCES imports(id),
  old_id INTEGER NOT NULL,
  row_json TEXT NOT NULL,
  PRIMARY KEY(import_id, old_id)
);
CREATE TABLE scan_runs(
  id INTEGER PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  planned_at TEXT NOT NULL,
  committed_at TEXT NOT NULL,
  added_works INTEGER NOT NULL,
  attached_copies INTEGER NOT NULL,
  marked_known INTEGER NOT NULL,
  marked_missing INTEGER NOT NULL
);
