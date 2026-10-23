"""Private, revisioned map geometry; inventory and location names remain in Grocy."""
import base64
import binascii
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import warnings

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError
from . import contracts as c

IMAGE_LIMIT = 10 * 1024 * 1024
PIXEL_LIMIT = 16_000_000
IMAGE_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def decode_image(data_url):
    """Decode a bounded raster then strip metadata and re-encode trusted pixels."""
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", data_url)
    if not match or len(match[2]) > 4 * ((IMAGE_LIMIT + 2) // 3):
        raise HTTPException(422, "背景須為 10 MiB 以下的 PNG、JPEG 或 WebP。")
    try:
        raw = base64.b64decode(match[2], validate=True)
        if len(raw) > IMAGE_LIMIT:
            raise ValueError("Image exceeds byte limit")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format != IMAGE_TYPES[match[1]] or getattr(source, "n_frames", 1) != 1:
                    raise ValueError("Only a matching single-frame raster is supported")
                width, height = source.size
                if width * height > PIXEL_LIMIT or min(width, height) < 200 or max(width, height) > 8000:
                    raise ValueError("Image dimensions outside canvas limits")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                pixels = oriented.convert("RGBA" if "A" in oriented.getbands() else "RGB")
                # A fresh image discards EXIF, textual chunks and other private metadata.
                clean = Image.new(pixels.mode, pixels.size)
                clean.paste(pixels)
                output = BytesIO()
                clean.save(output, format="PNG")
                return output.getvalue(), {"width": clean.width, "height": clean.height}
    except (ValueError, OSError, UnidentifiedImageError, binascii.Error, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "背景圖片無效；尺寸須為 200–8000 像素、總計不超過 1600 萬像素。") from None


class FloorplanStore:
    def __init__(self, database, db_path, lock):
        self.database, self.lock = database, lock
        self.directory = Path(db_path).parent / "floorplans"
        with database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS floorplan (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, geometry TEXT NOT NULL, image TEXT)")

    @staticmethod
    def empty():
        return {"revision": 0, "title": "家的地圖", "canvas": {"width": 1000, "height": 800}, "rooms": [], "spots": [], "image_url": None}

    def read(self, db=None):
        if db is None:
            with self.database() as connection:
                return self.read(connection)
        row = db.execute("SELECT revision,geometry,image FROM floorplan WHERE id=1").fetchone()
        if row is None:
            return self.empty()
        return {**json.loads(row[1]), "revision": row[0], "image_url": f"{c.FLOORPLAN_IMAGE}?revision={row[0]}" if row[2] else None}

    def image_path(self, name):
        # Names are exclusively server-generated; defend the disk boundary as well.
        if not name or not re.fullmatch(r"[0-9a-f]{32}\.png", name):
            raise HTTPException(404, "尚未設定背景圖片。")
        return self.directory / name

    def save(self, body, locations):
        data = body.model_dump(exclude={"revision", "image_data_url"})
        if any(spot.location_id not in {row["id"] for row in locations} for spot in body.spots):
            raise HTTPException(422, "圖釘的位置已不存在，請重新整理 Grocy 位置。")
        image_bytes = None
        if body.image_data_url is not None:
            image_bytes, data["canvas"] = decode_image(body.image_data_url)
        new_path = None
        with self.lock, self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT revision,image FROM floorplan WHERE id=1").fetchone()
            revision, old_name = old if old else (0, None)
            if revision != body.revision:
                raise HTTPException(409, "地圖已由其他裝置更新；請保留草稿並重新載入後再合併。")
            image_name = old_name
            if "image_data_url" in body.model_fields_set:
                image_name = None
                if image_bytes is not None:
                    self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    image_name = secrets.token_hex(16) + ".png"
                    new_path = self.image_path(image_name)
                    descriptor = os.open(new_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(image_bytes)
            try:
                db.execute("INSERT INTO floorplan VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,geometry=excluded.geometry,image=excluded.image",
                           (revision + 1, json.dumps(data, ensure_ascii=False), image_name))
                db.commit()
            except Exception:
                if new_path:
                    new_path.unlink(missing_ok=True)
                raise
            if old_name and old_name != image_name:
                self.image_path(old_name).unlink(missing_ok=True)
            return self.read(db)

    def image(self):
        with self.lock, self.database() as db:
            row = db.execute("SELECT image FROM floorplan WHERE id=1").fetchone()
            path = self.image_path(row[0] if row else None)
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                raise HTTPException(404, "尚未設定背景圖片。") from None
        return Response(content, media_type="image/png", headers={"Cache-Control": "no-store"})


def register_floorplan(app, authenticated, database, db_path, lock):
    store = FloorplanStore(database, db_path, lock)
    router = APIRouter(dependencies=[Depends(authenticated)])

    def locations():
        return app.state.grocy.locations()

    @router.get(c.FLOORPLAN)
    def get_plan():
        with lock:
            return {**store.read(), "locations": locations()}

    @router.put(c.FLOORPLAN)
    def save_plan(body: c.SavePlan):
        with lock:
            current_locations = locations()
            return {**store.save(body, current_locations), "locations": current_locations}

    @router.get(c.FLOORPLAN_IMAGE)
    def get_image():
        return store.image()

    @router.post(c.LOCATIONS)
    def create_location(body: c.LocationName):
        with lock:
            existing = next((row for row in locations() if row["name"] == body.name), None)
            return existing or app.state.grocy.create_location(body.name)

    @router.patch(c.LOCATIONS + "/{location_id}")
    def rename_location(location_id: int, body: c.LocationName):
        with lock:
            current = locations()
            existing = next((row for row in current if row["id"] == location_id), None)
            if existing is None:
                raise HTTPException(404, "找不到這個 Grocy 位置。")
            if any(row["name"] == body.name and row["id"] != location_id for row in current):
                raise HTTPException(409, "已存在同名位置，請使用其他名稱。")
            return existing if existing["name"] == body.name else app.state.grocy.rename_location(location_id, body.name)

    app.include_router(router)
    return store
