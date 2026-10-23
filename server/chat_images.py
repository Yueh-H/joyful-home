"""Decode bounded raster attachments; persist canonical pixels without private EXIF."""
import base64
import binascii
import hashlib
from io import BytesIO
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from . import contracts as c
from .chat_store import ChatStoreError

FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def decode_images(images):
    decoded = []
    for index, image in enumerate(images):
        try:
            raw = base64.b64decode(image.data, validate=True)
            if not raw or len(raw) > c.CHAT_IMAGE_MAX_BYTES:
                raise ValueError()
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as opened:
                    if opened.format != FORMATS[image.mime_type] or getattr(opened, "is_animated", False):
                        raise ValueError()
                    width, height = opened.size
                    if max(width, height) > 4096 or width * height > 12_000_000:
                        raise ValueError()
                    opened.verify()
                with Image.open(BytesIO(raw)) as opened:
                    opened.load()
                    clean = ImageOps.exif_transpose(opened)
                    clean = clean.convert("RGBA" if image.mime_type != "image/jpeg" and "A" in clean.getbands() else "RGB")
                    clean.info.clear()
                    output = BytesIO()
                    clean.save(output, format=FORMATS[image.mime_type], **({"quality": 90} if image.mime_type != "image/png" else {}))
                    data = output.getvalue()
                    if len(data) > c.CHAT_IMAGE_MAX_BYTES:
                        raise ValueError()
                    decoded.append({"id": str(index), "mime_type": image.mime_type, "data": data,
                                    "width": clean.width, "height": clean.height,
                                    "fingerprint": hashlib.sha256(raw).hexdigest()})
        except (ValueError, KeyError, SyntaxError, OSError, binascii.Error, UnidentifiedImageError,
                Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ChatStoreError("圖片格式不正確或超過限制：僅支援 PNG、JPEG、WebP；每張 2 MiB、最長邊 4096、最多 1200 萬像素，不支援動態圖片。", 422) from None
    return decoded
