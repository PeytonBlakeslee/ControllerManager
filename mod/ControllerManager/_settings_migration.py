"""One-time migration to the final ControllerManager settings filename."""
from pathlib import Path
import os
import tempfile

def migrate_settings(settings_dir):
    root = Path(settings_dir)
    old = root / "2ControllerManagerBeta.json"
    new = root / "ControllerManager.json"
    if new.exists() or not old.is_file():
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".ControllerManager.", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            target.write(old.read_bytes())
            target.flush()
            os.fsync(target.fileno())
        if not new.exists():
            os.replace(temporary, new)
            temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
