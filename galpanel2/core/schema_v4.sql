CREATE TABLE work_info(
  work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  summary TEXT,
  summary_source TEXT CHECK(summary_source IS NULL OR summary_source IN ('dlsite','vndb','bangumi','user')),
  release_date TEXT CHECK(release_date IS NULL OR release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  release_source TEXT CHECK(release_source IS NULL OR release_source IN ('dlsite','vndb','bangumi')),
  cover_source TEXT CHECK(cover_source IS NULL OR cover_source IN ('dlsite','vndb','bangumi')),
  updated_at TEXT NOT NULL
);
CREATE TABLE work_titles(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  title TEXT NOT NULL CHECK(length(trim(title))>0),
  title_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('original','translated','alias')),
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi')),
  PRIMARY KEY(work_id, title_key)
);
CREATE TABLE external_links(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi')),
  site_id TEXT NOT NULL,
  bound_at TEXT NOT NULL,
  PRIMARY KEY(work_id, site),
  UNIQUE(site, site_id)
);
CREATE TABLE metadata_snapshots(
  id INTEGER PRIMARY KEY,
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi')),
  site_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX idx_snapshots_item ON metadata_snapshots(site, site_id);
CREATE TABLE brand_links(
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb')),
  site_id TEXT NOT NULL,
  brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
  PRIMARY KEY(site, site_id)
);
CREATE TABLE source_tags(
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb')),
  tag_key TEXT NOT NULL,
  text TEXT NOT NULL,
  suggestion TEXT NOT NULL,
  category TEXT,
  action TEXT CHECK(action IS NULL OR action IN ('map','ignore')),
  tag_id INTEGER REFERENCES tags(id) ON DELETE SET NULL,
  decided_at TEXT,
  PRIMARY KEY(site, tag_key)
);
CREATE TABLE work_source_tags(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  site TEXT NOT NULL,
  tag_key TEXT NOT NULL,
  PRIMARY KEY(work_id, site, tag_key),
  FOREIGN KEY(site, tag_key) REFERENCES source_tags(site, tag_key) ON DELETE CASCADE
);
CREATE INDEX idx_work_source_tags_tag ON work_source_tags(site, tag_key);
CREATE TABLE work_tag_blocks(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY(work_id, tag_id)
);
