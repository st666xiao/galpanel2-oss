"""Short SQLite connections, a lifetime OS writer lock and nested transactions."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid

from galpanel2 import __version__
from .domain import LibraryLocked, NotALibrary, TargetExists, NeedsUpgrade, UpgradeResult, utc_now

APPLICATION_ID = 1196437506
SCHEMA_VERSION = 6
_held: set[str] = set()
_held_guard = threading.Lock()


def validate_target(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() != ".galdb":
        raise ValueError("Library filename must end in .galdb")
    if not p.parent.is_dir():
        raise ValueError("Library parent directory must already exist")
    return str(p.absolute())


def _connect(path: str, readonly: bool = False):
    c = sqlite3.connect(Path(path).as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                        uri=True, isolation_level=None, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA synchronous=FULL")
    if readonly:
        c.execute("PRAGMA query_only=ON")
    return c


def _identity(c):
    try:
        if c.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or \
                c.execute("PRAGMA user_version").fetchone()[0] not in range(1, SCHEMA_VERSION + 1):
            raise NotALibrary("Not a GalPanel 2 library")
        info = dict(c.execute("SELECT key,value FROM library_info"))
        uuid.UUID(info["library_uuid"])
        return info["library_uuid"]
    except (sqlite3.DatabaseError, KeyError, ValueError) as e:
        raise NotALibrary("Invalid GalPanel 2 library") from e


class _WriterLock:
    def __init__(self, path):
        self.key = os.path.normcase(os.path.realpath(path))
        self.file = None
        with _held_guard:
            if self.key in _held:
                raise LibraryLocked("Library already has a writer")
            _held.add(self.key)
        try:
            self.file = open(path + ".lock", "a+b")
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.file.fileno()).st_size == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                # Windows can retain a terminated process's handle briefly after
                # wait() returns. Bound that release grace period to half a second.
                deadline = time.monotonic() + .5
                while True:
                    try:
                        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.02)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as e:
            self.release()
            if isinstance(e, OSError):
                raise LibraryLocked("Library already has a writer") from e
            raise

    def release(self):
        if self.file is not None:
            self.file.close()  # OS closes/releases locks even on process death.
            self.file = None
        with _held_guard:
            _held.discard(self.key)


class Library:
    def __init__(self, path, library_uuid, readonly, writer_lock=None, schema_version=SCHEMA_VERSION):
        self.path = path
        self.uuid = library_uuid
        self.readonly = readonly
        self.schema_version = schema_version
        self._writer_lock = writer_lock
        self._mutex = threading.RLock()
        self._local = threading.local()
        self._closed = False

    @contextmanager
    def connection(self):
        with self._mutex:
            if self._closed:
                raise ValueError("Library is closed")
            active = getattr(self._local, "connection", None)
            if active is not None:
                yield active
                return
            c = _connect(self.path, self.readonly)
            try:
                yield c
            finally:
                c.close()

    @contextmanager
    def transaction(self):
        if self.readonly:
            raise ValueError("Library is read-only")
        with self._mutex:
            if self._closed:
                raise ValueError("Library is closed")
            active = getattr(self._local, "connection", None)
            if active is not None:
                # A failed nested operation must roll back even when its caller catches it.
                active.execute("SAVEPOINT nested_write")
                try:
                    yield active
                except BaseException:
                    active.execute("ROLLBACK TO nested_write")
                    active.execute("RELEASE nested_write")
                    raise
                else:
                    active.execute("RELEASE nested_write")
                return
            c = _connect(self.path)
            self._local.connection = c
            try:
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
            finally:
                self._local.connection = None
                c.close()

    def backup(self, target: str) -> None:
        """Snapshot via SQLite backup; exclusively create target, remove on failure."""
        with open(target, "xb"):
            pass
        try:
            with self.connection() as source:
                dest = sqlite3.connect(target)
                try:
                    source.backup(dest)
                finally:
                    dest.close()
            with open(target, "r+b") as fh:
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            os.unlink(target)
            raise

    def close(self):
        with self._mutex:
            if not self._closed:
                self._closed = True
                if self._writer_lock:
                    self._writer_lock.release()
                    self._writer_lock = None

    def __enter__(self):
        if self._closed:
            raise ValueError("Library is closed")
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        lock = getattr(self, "_writer_lock", None)
        if lock:
            lock.release()


def _initialize(path):
    """Create a private scratch DB; caller alone owns publication/cleanup."""
    c = sqlite3.connect(path, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=5000")
        c.executescript("BEGIN IMMEDIATE;\n" + Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                        + Path(__file__).with_name("schema_v2.sql").read_text(encoding="utf-8")
                        + Path(__file__).with_name("schema_v3.sql").read_text(encoding="utf-8")
                        + Path(__file__).with_name("schema_v4.sql").read_text(encoding="utf-8")
                        + Path(__file__).with_name("schema_v5.sql").read_text(encoding="utf-8")
                        + Path(__file__).with_name("schema_v6.sql").read_text(encoding="utf-8"))
        c.execute(f"PRAGMA application_id={APPLICATION_ID}")
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        u = str(uuid.uuid4())
        c.executemany("INSERT INTO library_info VALUES(?,?)",
                      [("library_uuid", u), ("created_at", utc_now()), ("app_version", __version__)])
        c.execute("COMMIT")
        return u
    finally:
        c.close()


def create_library(path: str) -> Library:
    path = validate_target(path)
    if os.path.lexists(path):
        raise TargetExists(path)
    lock = _WriterLock(path)
    scratch = None
    try:
        fd, scratch = tempfile.mkstemp(prefix=".gp2-create-", dir=str(Path(path).parent))
        os.close(fd)
        u = _initialize(scratch)
        try:
            os.link(scratch, path)  # Atomic, never replaces a target created meanwhile.
        except FileExistsError as e:
            raise TargetExists(path) from e
        return Library(path, u, False, lock, SCHEMA_VERSION)
    except BaseException:
        lock.release()
        raise
    finally:
        if scratch and os.path.exists(scratch):
            os.unlink(scratch)


def _inspect(path):
    try:
        c = _connect(path, True)
        try:
            return _identity(c), c.execute("PRAGMA user_version").fetchone()[0]
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        raise NotALibrary("Invalid SQLite library") from e


def _library_path(path):
    path = str(Path(path).absolute())
    if Path(path).suffix.lower() != ".galdb":
        raise ValueError("Library filename must end in .galdb")
    if not os.path.isfile(path):
        raise NotALibrary("Library file does not exist")
    return path


def open_library(path: str, readonly: bool = False, *, recover: bool = True) -> Library:
    path = _library_path(path)
    u, version = _inspect(path)
    if version < SCHEMA_VERSION and not readonly:
        # Preflight is read-only: a declined upgrade never even creates a lock file.
        raise NeedsUpgrade(f"Library needs version {SCHEMA_VERSION} upgrade", version=version)
    if readonly:
        return Library(path, u, True, schema_version=version)
    lock = _WriterLock(path)
    lib = None
    try:
        c = _connect(path)
        try:
            u = _identity(c)
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise NeedsUpgrade(f"Library needs version {SCHEMA_VERSION} upgrade", version=version)
            c.execute("PRAGMA journal_mode=DELETE")
        finally:
            c.close()
        lib = Library(path, u, False, lock, version)
        from .play import recover_sessions
        if recover:
            recover_sessions(lib)
        return lib
    except BaseException:
        if lib is not None:
            lib.close()
        else:
            lock.release()
        raise


def upgrade_library(path: str) -> UpgradeResult:
    path = _library_path(path)
    _, version = _inspect(path)
    if version == SCHEMA_VERSION:
        raise ValueError(f"already version {SCHEMA_VERSION}")
    lock = _WriterLock(path)
    partial = None
    c = None
    try:
        c = _connect(path)
        _identity(c)
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            raise ValueError(f"already version {SCHEMA_VERSION}")
        stamp = utc_now().replace("-", "").replace(":", "")
        backup = path + f".v{version}-" + stamp + ".bak"
        if os.path.lexists(backup):
            raise TargetExists(backup)
        candidate = backup + ".partial"
        # Never overwrite another file, even if a previous attempt left a partial.
        with open(candidate, "xb"):
            pass
        partial = candidate
        dest = sqlite3.connect(partial)
        try:
            c.backup(dest)
        finally:
            dest.close()
        with open(partial, "r+b") as f:
            f.flush()
            os.fsync(f.fileno())
        dest = sqlite3.connect(Path(partial).as_uri() + "?mode=ro", uri=True)
        try:
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or \
                    dest.execute("PRAGMA user_version").fetchone()[0] != version:
                raise NotALibrary("Invalid upgrade backup")
        finally:
            dest.close()
        if os.path.lexists(backup):
            raise TargetExists(backup)
        os.replace(partial, backup)
        partial = None
        c.execute("BEGIN IMMEDIATE")
        try:
            # executescript commits implicitly; execute each DDL to keep rollback atomic.
            schemas = tuple(f"schema_v{v}.sql" for v in range(version + 1, SCHEMA_VERSION + 1))
            for schema in schemas:
                ddl = Path(__file__).with_name(schema).read_text(encoding="utf-8")
                for statement in ddl.split(";"):
                    if statement.strip():
                        c.execute(statement)
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            c.executemany("INSERT OR REPLACE INTO library_info VALUES(?,?)",
                          [("schema_upgraded_at", utc_now()), ("app_version", __version__)])
            pause = os.environ.get("GALPANEL2_TEST_PAUSE_FILE")
            if pause:
                Path(pause).touch()
                time.sleep(120)
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        return UpgradeResult(path, backup, version, SCHEMA_VERSION)
    finally:
        if c is not None:
            c.close()
        if partial is not None and os.path.exists(partial):
            os.unlink(partial)
        lock.release()
