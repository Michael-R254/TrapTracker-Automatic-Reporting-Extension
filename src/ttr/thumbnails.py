"""Small JPEG data-URI thumbnails for on-disk images.

One implementation, two consumers: the report generator's Appendix-A thumbnail
grid and the ingest page's live strip. Neither may raise, and neither may leak a
filesystem path into rendered output.

Pillow lives in the ``[enrich]`` extra, so on a core-only install this returns
``None`` and every caller MUST have a caption-only fallback. The import is
deferred to call time so ``import ttr.thumbnails`` stays free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union


def thumb_data_uri(path: Union[str, Path, None], max_px: int = 200) -> Optional[str]:
    """A ``data:image/jpeg;base64,...`` URI for an image on disk.

    Returns ``None`` — never raises — when the path is empty, the file is
    missing, Pillow is absent, or the bytes are not a decodable image. The path
    itself never appears in the result or in an exception message.
    """
    if not path:
        return None
    try:
        p = Path(path)
        if not p.is_file():
            return None
        import base64
        import io

        from PIL import Image
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
