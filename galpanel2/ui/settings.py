import json
import os
from pathlib import Path
import tempfile

from galpanel2.core import storage

DEFAULTS = dict(recent=[], theme="media", show_mascot=True, window_position=None, native_frame=False,
                cold_root=storage.DEFAULT_COLD_ROOT)


def settings_dir() -> Path:
    """The one directory used for settings.json and as the fallback library location."""
    base = os.environ.get("GALPANEL2_SETTINGS_DIR") or str(
        Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "GalPanel2")
    return Path(base)


def default_library_path() -> str:
    """Where 「开始使用」 puts a brand new library."""
    override = os.environ.get("GALPANEL2_DEFAULT_LIBRARY")
    if override:
        return override
    if os.path.isdir("D:\\"):
        return r"D:\GalPanel2\library.galdb"
    return str(settings_dir() / "library.galdb")


class Settings:
    def __init__(self):
        self.path = settings_dir() / "settings.json"
        self.data = dict(DEFAULTS, recent=[])
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.data["recent"] = list(dict.fromkeys(p for p in saved.get("recent", []) if isinstance(p, str)))[:8]
            self.data["theme"] = saved.get("theme") if saved.get("theme") in ("day", "night", "media") else "media"
            self.data["show_mascot"] = saved.get("show_mascot", True) is not False
            # Only real booleans are accepted; anything else (1, "true", None…) is False.
            self.data["native_frame"] = saved.get("native_frame") is True
            cold_root = saved.get("cold_root")
            if isinstance(cold_root, str) and cold_root.strip():
                self.data["cold_root"] = cold_root
            pos = saved.get("window_position")
            if isinstance(pos, list) and len(pos) == 4 and all(type(x) is int for x in pos):
                self.data["window_position"] = pos
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({k: self.data[k] for k in DEFAULTS}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def remember(self, path):
        self.data["recent"] = [path] + [p for p in self.data["recent"] if p != path]
        self.data["recent"] = self.data["recent"][:8]
        self.save()
