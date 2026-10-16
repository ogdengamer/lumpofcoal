import io
import struct

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageStat
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import Person
from app.photos import FULL_MAX_PX, THUMB_MAX_PX, PhotoError, process
from app.visibility import GiftRepo

ORIENTATION = 0x0112
RED, WHITE = (255, 0, 0), (255, 255, 255)

# For each EXIF orientation, the transpose that turns an upright image into the
# sideways pixels a camera would store (the inverse of what exif_transpose does).
STORE_AS = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}


def upright(size=(300, 200)) -> Image.Image:
    """White landscape image with a red block in its top-left corner."""
    img = Image.new("RGB", size, WHITE)
    img.paste(RED, (0, 0, size[0] // 5, size[1] // 5))
    return img


def encode(img: Image.Image, fmt="JPEG", orientation=None, **exif_tags) -> bytes:
    exif = Image.Exif()
    if orientation:
        exif[ORIENTATION] = orientation
    for tag, value in exif_tags.items():
        exif[int(tag.lstrip("t"))] = value
    out = io.BytesIO()
    img.save(out, format=fmt, exif=exif.tobytes(), quality=95)
    return out.getvalue()


def sideways(orientation: int, fmt="JPEG") -> bytes:
    img = upright()
    if STORE_AS[orientation] is not None:  # FLIP_LEFT_RIGHT == 0, so no truthiness test
        img = img.transpose(STORE_AS[orientation])
    return encode(img, fmt, orientation=orientation)


def is_red(px) -> bool:
    return px[0] > 200 and px[1] < 80 and px[2] < 80


def assert_upright(webp: bytes, size=(300, 200)):
    img = Image.open(io.BytesIO(webp)).convert("RGB")
    assert img.size == size
    w, h = img.size
    assert is_red(img.getpixel((5, 5))), "marker should be top-left"
    assert not is_red(img.getpixel((w - 5, 5))), "image is mirrored or rotated"
    assert not is_red(img.getpixel((5, h - 5))), "image is flipped or rotated"


# --- orientation ---------------------------------------------------------

@pytest.mark.parametrize("orientation", range(1, 9))
def test_jpeg_exif_orientation_is_applied(orientation):
    result = process(sideways(orientation))
    assert_upright(result.full)
    assert_upright(result.thumb)


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_heic_orientation_is_applied_exactly_once(orientation):
    # pillow-heif rotates while decoding and resets the tag; we must not rotate again.
    assert_upright(process(sideways(orientation, fmt="HEIF")).full)


def test_portrait_phone_photo_comes_out_portrait():
    # A typical phone portrait: stored 4032x3024 landscape with orientation 6.
    stored = upright((3024, 4032)).transpose(Image.Transpose.ROTATE_90)
    result = process(encode(stored, orientation=6))
    assert (result.width, result.height) == (1200, 1600)


# --- size ----------------------------------------------------------------

@pytest.mark.parametrize("size, full, thumb", [
    pytest.param((4032, 3024), (1600, 1200), (400, 300), id="4:3-landscape"),
    pytest.param((3024, 4032), (1200, 1600), (300, 400), id="4:3-portrait"),
    pytest.param((4000, 2250), (1600, 900), (400, 225), id="16:9"),
    pytest.param((9000, 1500), (1600, 267), (400, 67), id="panorama"),
    pytest.param((800, 352), (800, 352), (400, 176), id="small-import-not-enlarged"),
])
def test_resized_to_fit_keeping_aspect_ratio(size, full, thumb):
    result = process(encode(upright(size)))
    assert Image.open(io.BytesIO(result.full)).size == full
    assert Image.open(io.BytesIO(result.thumb)).size == thumb
    assert max(full) <= FULL_MAX_PX and max(thumb) <= THUMB_MAX_PX


# --- metadata ------------------------------------------------------------

def test_exif_and_gps_are_stripped():
    gps_ifd = 0x8825
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"          # Make
    exif[0x0110] = "PhoneModel"          # Model
    exif.get_ifd(gps_ifd)[2] = (45.0, 31.0, 0.0)  # GPSLatitude
    out = io.BytesIO()
    upright().save(out, format="JPEG", exif=exif.tobytes())
    assert b"PhoneMaker" in out.getvalue()

    result = process(out.getvalue())
    for data in (result.full, result.thumb):
        img = Image.open(io.BytesIO(data))
        assert img.format == "WEBP"
        assert len(img.getexif()) == 0
        assert "exif" not in img.info and "icc_profile" not in img.info
        assert b"PhoneMaker" not in data and b"Exif" not in data


def test_heic_is_accepted_and_converted():
    result = process(encode(upright(), fmt="HEIF"))
    assert Image.open(io.BytesIO(result.full)).format == "WEBP"


def test_png_with_transparency_is_accepted():
    img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="PNG")
    assert Image.open(io.BytesIO(process(out.getvalue()).full)).mode == "RGBA"


@pytest.mark.parametrize("data", [
    pytest.param(b"definitely not an image", id="garbage"),
    pytest.param(b"GIF89a" + b"\x00" * 50, id="truncated-gif"),
])
def test_non_images_are_rejected(data):
    with pytest.raises(PhotoError):
        process(data)


def test_unsupported_format_is_rejected():
    out = io.BytesIO()
    upright().save(out, format="GIF")
    with pytest.raises(PhotoError, match="Unsupported"):
        process(out.getvalue())


# --- HTTP: upload and visibility ------------------------------------------

USERS = {"abel@example.com": "Abel", "jules@example.com": "Jules"}


@pytest.fixture
def clients(tmp_path):
    def make(email):
        return TestClient(create_app(Settings(users=USERS, dev_user=email,
                                              data_dir=str(tmp_path))))
    return make("abel@example.com"), make("jules@example.com")


def upload(client, data=None):
    resp = client.post("/photos", files={"file": ("p.jpg", data or encode(upright()),
                                                  "image/jpeg")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_upload_returns_servable_urls(clients):
    abel, _ = clients
    photo = upload(abel)
    resp = abel.get(photo["thumb"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"
    assert "private" in resp.headers["cache-control"]
    assert abel.get(photo["full"]).status_code == 200
    assert not photo["full"].endswith(".webp")


def test_unattached_upload_is_private_to_uploader(clients):
    abel, jules = clients
    photo = upload(abel)
    assert jules.get(photo["thumb"]).status_code == 404


def test_photo_of_gift_for_viewer_returns_404(clients):
    abel, jules = clients
    photo = upload(jules)
    app = jules.app
    with app.state.sessionmaker() as session:
        ids = dict(session.execute(select(Person.name, Person.id)).all())
        repo = GiftRepo(session, ids["Jules"])
        gift = repo.create(title="Made-up gift", recipient_person_id=ids["Abel"])
        assert repo.attach_photo(gift.id, photo["id"])
        session.commit()

    assert jules.get(photo["full"]).status_code == 200
    assert abel.get(photo["full"]).status_code == 404
    assert abel.get(photo["thumb"]).status_code == 404


@pytest.mark.parametrize("path", [
    "/photos/../../gifts.db/full",
    "/photos/" + "a" * 32 + "/original",
    "/photos/" + "a" * 32 + "/full",
    "/photos/not-an-id/thumb",
])
def test_bad_or_unknown_photo_paths_are_404(clients, path):
    abel, _ = clients
    assert abel.get(path).status_code == 404


def test_upload_rejects_non_images(clients):
    abel, _ = clients
    resp = abel.post("/photos", files={"file": ("x.jpg", b"nope", "image/jpeg")})
    assert resp.status_code == 415


# --- colour profiles -----------------------------------------------------

def icc_profile(primaries, gamma: float = 2.2) -> bytes:
    """A minimal ICC v2 RGB display profile: XYZ primaries (D50) plus a gamma curve.

    Enough for LittleCMS to convert with; built here so no third-party profile
    file has to ship with the tests."""
    def fixed(v):
        return struct.pack(">i", round(v * 65536))

    def xyz(x, y, z):
        return b"XYZ " + bytes(4) + fixed(x) + fixed(y) + fixed(z)

    curve = b"curv" + bytes(4) + struct.pack(">IH", 1, round(gamma * 256)) + bytes(2)
    name = b"Test wide gamut\0"
    desc = (b"desc" + bytes(4) + struct.pack(">I", len(name)) + name
            + bytes(4) + bytes(4) + bytes(2) + bytes(1) + bytes(67))
    red, green, blue = primaries
    tags = [(b"desc", desc), (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
            (b"rXYZ", xyz(*red)), (b"gXYZ", xyz(*green)), (b"bXYZ", xyz(*blue)),
            (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve),
            (b"cprt", b"text" + bytes(4) + b"No copyright, use freely\0")]
    offset, table, data = 128 + 4 + 12 * len(tags), b"", b""
    for sig, body in tags:
        body += bytes(-len(body) % 4)
        table += sig + struct.pack(">II", offset + len(data), len(body))
        data += body
    size = offset + len(data)
    header = (struct.pack(">I", size) + bytes(4) + bytes([2, 0x10, 0, 0]) + b"mntrRGB XYZ "
              + bytes(12) + b"acsp" + bytes(24) + bytes(4)
              + fixed(0.9642) + fixed(1.0) + fixed(0.8249) + bytes(4) + bytes(16) + bytes(28))
    assert len(header) == 128
    return header + struct.pack(">I", len(tags)) + table + data


# Display P3 (the colour space phone cameras save in), adapted to D50.
P3_ICC = icc_profile([(0.5151, 0.2412, -0.0011), (0.2920, 0.6922, 0.0419),
                      (0.1571, 0.0666, 0.7841)])


def saturation(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("HSV")).mean[1]


def test_wide_gamut_photo_is_converted_to_srgb():
    """P3 pixel values shown as sRGB look washed out; conversion must re-saturate them."""
    img = Image.new("RGB", (64, 64), (60, 170, 90))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=100, icc_profile=P3_ICC)

    converted = Image.open(io.BytesIO(process(out.getvalue()).full)).convert("RGB")
    assert "icc_profile" not in converted.info
    assert saturation(converted) > saturation(img) + 5
