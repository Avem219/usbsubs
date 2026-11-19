"""USB root detection and relative-path helpers. No drive letters or usernames are ever stored."""
import os
from pathlib import Path

WORKSPACE_NAME = "USB_SUBTITLE"


class RootNotFound(RuntimeError):
    pass


def find_root(start=None, override=None) -> Path:
    """Return the USB root (the folder that CONTAINS USB_SUBTITLE/).

    Order: explicit --root, USB_SUBTITLE_ROOT env var, then walk up from this file.
    """
    env = override or os.environ.get("USB_SUBTITLE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(start or __file__).resolve()
    for p in [here, *here.parents]:
        if p.name.upper() == WORKSPACE_NAME and (p / "app" / "usb_subtitle").is_dir():
            return p.parent
    raise RootNotFound(
        "Could not find the USB_SUBTITLE folder above this program. "
        "Run it from the USB drive, or pass --root <folder containing USB_SUBTITLE>."
    )


class Workspace:
    def __init__(self, root: Path, movies_name: str = "MOVIES"):
        self.root = Path(root)
        self.movies_name = movies_name
        self.base = self.root / WORKSPACE_NAME
        self.config_dir = self.base / "config"
        self.db_dir = self.base / "database"
        self.log_dir = self.base / "logs"
        self.temp_dir = self.base / "temp"
        self.models_dir = self.base / "models"   # only used in portable-model mode
        self.settings_path = self.config_dir / "settings.json"
        self.db_path = self.db_dir / "history.db"
        self.log_path = self.log_dir / "application.log"

    @property
    def movies_dir(self) -> Path:
        return self.root / self.movies_name

    def ensure(self):
        for d in (self.config_dir, self.db_dir, self.log_dir, self.temp_dir, self.movies_dir):
            d.mkdir(parents=True, exist_ok=True)

    def is_available(self) -> bool:
        """False once the drive has been unplugged."""
        try:
            os.listdir(self.base)
            return True
        except OSError:
            return False

    def rel(self, path) -> str:
        """Path relative to the USB root, always with forward slashes."""
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def abs(self, rel: str) -> Path:
        return self.root / Path(*rel.split("/"))
