"""Immutable API values and Windows lexical identities (no disk access)."""
from dataclasses import dataclass
import ntpath
import re
from datetime import datetime, timezone


class GalPanelError(Exception):
    pass


class NotALibrary(GalPanelError):
    pass


class NeedsUpgrade(GalPanelError):
    def __init__(self, message: str = "", version: int | None = None):
        self.version = version
        super().__init__(message)


class DuplicateName(GalPanelError):
    def __init__(self, message: str, existing_id: int | None = None):
        self.existing_id = existing_id
        super().__init__(message)


class DuplicateLink(GalPanelError):
    def __init__(self, message: str, work_id: int | None = None):
        self.work_id = work_id
        super().__init__(message)


class SiteError(GalPanelError):
    def __init__(self, message: str, site: str | None = None):
        self.site = site
        super().__init__(message)


class SiteNotFound(SiteError):
    pass


class SiteUnreachable(SiteError):
    """连接失败或超时。站点返回了 HTTP 状态（含 404、500、429）时不是这种错误。"""


class SessionActive(GalPanelError):
    pass


class NotLaunchable(GalPanelError):
    def __init__(self, reason: str, message: str = ""):
        if reason not in {"archive", "copy_missing", "no_exe", "bad_exe_path", "exe_missing", "no_copy"}:
            raise ValueError("Unknown launch reason")
        self.reason = reason
        super().__init__(message or reason)


class LaunchError(GalPanelError):
    pass


@dataclass(frozen=True)
class UpgradeResult:
    path: str
    backup_path: str
    from_version: int
    to_version: int


class LibraryLocked(GalPanelError):
    pass


class TargetExists(GalPanelError):
    pass


class WriteConflict(GalPanelError):
    pass


class PlanConflict(GalPanelError):
    pass


class Cancelled(GalPanelError):
    pass


class LegacyError(GalPanelError):
    pass


def path_key(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("Expected an absolute Windows path")
    p = path.replace("/", "\\")
    if p.startswith("\\\\.\\"):
        raise ValueError("Device paths are not library asset paths")
    if p.lower().startswith("\\\\?\\unc\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    drive, tail = ntpath.splitdrive(p)
    unc = drive.startswith("\\\\") and len(drive[2:].split("\\")) == 2 and all(drive[2:].split("\\"))
    if not (unc or (re.fullmatch(r"[A-Za-z]:", drive) and tail.startswith("\\"))):
        raise ValueError("Expected a drive-absolute or UNC path")
    key = ntpath.normcase(ntpath.normpath(p))
    return key if re.fullmatch(r"[a-z]:\\", key) else key.rstrip("\\")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_cancel(cancel) -> None:
    if cancel is not None and cancel():
        raise Cancelled("Operation cancelled; no changes committed")


@dataclass(frozen=True)
class Source:
    id: int
    path: str
    label: str
    last_scan_at: str | None


@dataclass(frozen=True)
class Copy:
    id: int
    kind: str
    path: str
    exe_relpath: str | None
    size_bytes: int | None
    availability: str
    checked_at: str | None
    source_id: int | None
    origin: str


@dataclass(frozen=True)
class WorkSummary:
    id: int
    title: str
    developer: str
    hidden: bool
    added_at: str
    has_cover: bool
    copy_count: int
    kinds: tuple[str, ...]
    availability: str


@dataclass(frozen=True)
class WorkDetail:
    id: int
    uuid: str
    title: str
    developer: str
    notes: str
    hidden: bool
    added_at: str
    revision: int
    cover_path: str | None
    copies: tuple[Copy, ...]


@dataclass(frozen=True)
class PlanItem:
    key: str
    path: str
    kind: str
    title: str
    developer: str
    exe_relpath: str | None
    size_bytes: int | None
    status: str
    candidates: tuple[int, ...]
    copy_id: int | None
    reason: str


@dataclass(frozen=True)
class ScanPlan:
    source_id: int
    root: str
    planned_at: str
    items: tuple[PlanItem, ...]
    errors: tuple[str, ...]
    ignored: int = 0


@dataclass(frozen=True)
class CommitResult:
    scan_run_id: int
    added_works: int
    attached_copies: int
    marked_known: int
    marked_missing: int


@dataclass(frozen=True)
class AvailabilityResult:
    checked: int
    ok: int
    missing: int
    error: int


@dataclass(frozen=True)
class LegacyInfo:
    path: str
    sha256: str
    columns: tuple[str, ...]
    games: int
    sources: int
    play_logs: int
    notes_nonempty: int
    bindings_nonempty: int


@dataclass(frozen=True)
class RejectedRow:
    old_id: int
    path: str
    reason: str


@dataclass(frozen=True)
class ImportReport:
    target_path: str
    imported: int
    rejected: tuple[RejectedRow, ...]
    covers_copied: int
    covers_missing: int
    sha256_before: str
    sha256_after: str
    conservation_errors: tuple[str, ...]


@dataclass(frozen=True)
class CompareReport:
    only_a: tuple[str, ...]
    only_b: tuple[str, ...]
    common: int
    field_diffs: dict[str, int]
