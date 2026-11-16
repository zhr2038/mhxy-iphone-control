import os
from pathlib import Path

SERVICE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MHXY" / "Control"
WORK = Path(os.environ.get("MHXY_WORK_DIR", SERVICE_DIR / "work")).expanduser().resolve()
FRAME_FILE = Path(os.environ.get("MHXY_FRAME_FILE", WORK / "iphone-live.frame")).expanduser().resolve()
