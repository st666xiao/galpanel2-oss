CREATE TABLE work_info_v6(
  work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
  summary TEXT,
  summary_source TEXT CHECK(summary_source IS NULL OR summary_source IN ('dlsite','vndb','bangumi','steam','f95','user')),
  release_date TEXT CHECK(release_date IS NULL OR release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  release_source TEXT CHECK(release_source IS NULL OR release_source IN ('dlsite','vndb','bangumi','steam','f95')),
  cover_source TEXT CHECK(cover_source IS NULL OR cover_source IN ('dlsite','vndb','bangumi','steam','f95')),
  updated_at TEXT NOT NULL
);
INSERT INTO work_info_v6(work_id,summary,summary_source,release_date,release_source,cover_source,updated_at)
SELECT work_id,summary,summary_source,release_date,release_source,cover_source,updated_at FROM work_info;
DROP TABLE work_info;
ALTER TABLE work_info_v6 RENAME TO work_info;

CREATE TABLE work_titles_v6(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  title TEXT NOT NULL CHECK(length(trim(title))>0),
  title_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('original','translated','alias')),
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi','steam','f95')),
  PRIMARY KEY(work_id, title_key)
);
INSERT INTO work_titles_v6(work_id,title,title_key,kind,site)
SELECT work_id,title,title_key,kind,site FROM work_titles;
DROP TABLE work_titles;
ALTER TABLE work_titles_v6 RENAME TO work_titles;

CREATE TABLE external_links_v6(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi','steam','f95')),
  site_id TEXT NOT NULL,
  bound_at TEXT NOT NULL,
  PRIMARY KEY(work_id, site),
  UNIQUE(site, site_id)
);
INSERT INTO external_links_v6(work_id,site,site_id,bound_at)
SELECT work_id,site,site_id,bound_at FROM external_links;
DROP TABLE external_links;
ALTER TABLE external_links_v6 RENAME TO external_links;

CREATE TABLE metadata_snapshots_v6(
  id INTEGER PRIMARY KEY,
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi','steam','f95')),
  site_id TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
INSERT INTO metadata_snapshots_v6(id,site,site_id,fetched_at,payload)
SELECT id,site,site_id,fetched_at,payload FROM metadata_snapshots;
DROP TABLE metadata_snapshots;
ALTER TABLE metadata_snapshots_v6 RENAME TO metadata_snapshots;
CREATE INDEX idx_snapshots_item ON metadata_snapshots(site, site_id);

CREATE TABLE brand_links_v6(
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi','steam','f95')),
  site_id TEXT NOT NULL,
  brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
  PRIMARY KEY(site, site_id)
);
INSERT INTO brand_links_v6(site,site_id,brand_id)
SELECT site,site_id,brand_id FROM brand_links;
DROP TABLE brand_links;
ALTER TABLE brand_links_v6 RENAME TO brand_links;

ALTER TABLE work_source_tags RENAME TO work_source_tags_v5;
ALTER TABLE source_tags RENAME TO source_tags_v5;
CREATE TABLE source_tags(
  site TEXT NOT NULL CHECK(site IN ('dlsite','vndb','bangumi','steam','f95')),
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
INSERT INTO source_tags(site,tag_key,text,suggestion,category,action,tag_id,decided_at)
SELECT site,tag_key,text,suggestion,category,action,tag_id,decided_at FROM source_tags_v5;
INSERT INTO work_source_tags(work_id,site,tag_key)
SELECT work_id,site,tag_key FROM work_source_tags_v5;
DROP TABLE work_source_tags_v5;
DROP TABLE source_tags_v5;
CREATE INDEX idx_work_source_tags_tag ON work_source_tags(site, tag_key);
