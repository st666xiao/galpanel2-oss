CREATE TABLE ignored_paths(
  path_key TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  scope TEXT NOT NULL CHECK(scope IN ('exact','tree')),
  reason TEXT NOT NULL CHECK(reason IN ('removed','user')),
  added_at TEXT NOT NULL
);
CREATE TABLE tidy_log(
  id INTEGER PRIMARY KEY,
  action TEXT NOT NULL CHECK(action IN ('remove','merge','retitle','ignore','legacy_summary')),
  at TEXT NOT NULL,
  backup_path TEXT,
  detail TEXT NOT NULL
);
