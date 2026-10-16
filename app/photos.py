"""Photo processing and storage.

Every upload is decoded, rotated upright, converted to sRGB, scaled down (never
cropped or enlarged) and re-encoded as WebP. Re-encoding without metadata
strips EXIF, including GPS. Order matters: phones save JPEGs sideways plus an
EXIF orientation tag, so the tag must be applied to the pixels *before* it is
discarded. (pillow-heif already applies HEIC rotation when decoding and resets
the tag, so the same step is a no-op there.)
"""

import io
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

FULL_MAX_PX = 1600
THUMB_MAX_PX = 400
WEBP_QUALITY = 82
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "MPO"}  # MPO: some phones' JPEGs
# Phones top out around 50-200 MP; refuse anything larger (decompression bombs).
Image.MAX_IMAGE_PIXELS = 220_000_000

SRGB = ImageCms.createProfile("sRGB")


class PhotoError(ValueError):
    pass


@dataclass(frozen=True)
class Processed:
    full: bytes
    thumb: bytes
    width: int
    height: int


def _to_srgb(img: Image.Image) -> Image.Image:
    """Convert from an embedded colour profile (e.g. Display P3) to sRGB.

    Dropping the profile without converting would make phone photos look washed out.
    """
    icc = img.info.get("icc_profile")
    if not icc or img.mode not in ("RGB", "RGBA", "CMYK"):
        return img
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.profileToProfile(img, src, SRGB,
                                         outputMode="RGBA" if img.mode == "RGBA" else "RGB")
    except (ImageCms.PyCMSError, OSError):
        return img


def _encode(img: Image.Image, max_px: int) -> bytes:
    img = img.copy()
    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)  # keeps aspect, never enlarges
    out = io.BytesIO()
    # No exif= / icc_profile= arguments, so no metadata is written.
    img.save(out, format="WEBP", quality=WEBP_QUALITY, method=6)
    return out.getvalue()


def process(data: bytes) -> Processed:
    if len(data) > MAX_UPLOAD_BYTES:
        raise PhotoError("Photo is too large")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ALLOWED_FORMATS:
            raise PhotoError(f"Unsupported image type: {img.format}")
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise PhotoError("Not a readable image") from exc

    img = ImageOps.exif_transpose(img)
    img = _to_srgb(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")

    full = _encode(img, FULL_MAX_PX)
    width, height = Image.open(io.BytesIO(full)).size
    return Processed(full=full, thumb=_encode(img, THUMB_MAX_PX), width=width, height=height)


class PhotoStore:
    """Processed photos on disk: <data>/photos/{full,thumb}/<id>.webp."""

    SIZES = ("full", "thumb")

    def __init__(self, data_dir: str):
        self.root = Path(data_dir) / "photos"
        for size in self.SIZES:
            (self.root / size).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def path(self, photo_id: str, size: str) -> Path:
        if size not in self.SIZES or not (len(photo_id) == 32 and photo_id.isalnum()):
            raise ValueError("bad photo reference")
        return self.root / size / f"{photo_id}.webp"

    def save(self, photo_id: str, processed: Processed) -> None:
        for size, data in (("full", processed.full), ("thumb", processed.thumb)):
            final = self.path(photo_id, size)
            tmp = final.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, final)  # atomic: never serve a half-written file

    def delete(self, photo_id: str) -> None:
        for size in self.SIZES:
            self.path(photo_id, size).unlink(missing_ok=True)
