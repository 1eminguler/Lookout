"""Test suite package bootstrap — keeps imports away from the user's live data dir."""
import os
import tempfile
from pathlib import Path

_BASE = Path(tempfile.gettempdir()) / "lookout_test_bootstrap"
os.environ.setdefault("LOOKOUT_DATA_DIR", str(_BASE / "data"))
os.environ.setdefault("LOOKOUT_PLUGIN_DIR", str(_BASE / "plugins"))
