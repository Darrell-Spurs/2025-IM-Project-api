import os
from pathlib import Path
from fastapi import HTTPException
from typing import Dict, Any

# Limit file serving to a safe base directory; set this to where your screenshots live.
FILES_BASE_DIR = Path(os.getenv("FILES_BASE_DIR", ".")).resolve()


def normalize_and_check(path_str: str) -> Path:
    """
    Normalize a DB path to an absolute path under FILES_BASE_DIR.
    Prevents path traversal and keeps downloads inside the allowed folder.
    """
    # If DB already stores absolute paths, we'll still enforce containment in BASE_DIR.
    p = (FILES_BASE_DIR / path_str).resolve() if not Path(path_str).is_absolute() else Path(path_str).resolve()
    try:
        p.relative_to(FILES_BASE_DIR)
    except Exception:
        # Path tries to escape the base directory
        raise HTTPException(status_code=400, detail="Illegal file path.")
    return p


def row_to_meta(row) -> Dict[str, Any]:
    # row: (id, user_id, timestamp, category, path)
    return {
        "id": row[0],
        "user_id": row[1],
        "timestamp": row[2].isoformat() if row[2] else None,
        "category": row[3],
        "path": row[4],
        # You can construct a download URL on your own domain if this runs behind a proxy:
        "download_url": f"/files/{row[0]}/download"
    }
