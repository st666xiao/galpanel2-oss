CREATE TABLE brands(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL CHECK(length(trim(name))>0),
  name_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE brand_aliases(
  brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
  alias TEXT NOT NULL CHECK(length(trim(alias))>0),
  alias_key TEXT NOT NULL UNIQUE,
  PRIMARY KEY(brand_id, alias_key)
);
CREATE TABLE work_brands(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY(work_id, brand_id)
);
CREATE INDEX idx_work_brands_brand ON work_brands(brand_id);
CREATE TABLE tag_groups(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  position INTEGER NOT NULL
);
CREATE TABLE tags(
  id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES tag_groups(id),
  name TEXT NOT NULL CHECK(length(trim(name))>0),
  name_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE work_tags(
  work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY(work_id, tag_id)
);
CREATE INDEX idx_work_tags_tag ON work_tags(tag_id);
CREATE TABLE brand_decisions(
  text_key TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('brand','ignore')),
  brand_id INTEGER REFERENCES brands(id) ON DELETE CASCADE,
  decided_at TEXT NOT NULL,
  CHECK((action='brand' AND brand_id IS NOT NULL) OR (action='ignore' AND brand_id IS NULL))
);
INSERT INTO tag_groups(id,name,position) VALUES(1,'题材',1),(2,'角色',2),(3,'系统',3),(4,'H 内容',4),(5,'其他',5);
