"""Application API and Grocy metadata contracts; no model-supplied URLs."""
from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

API = "/api"
SESSION = f"{API}/session"
LOGIN = f"{API}/login"
LOGOUT = f"{API}/logout"
STATE = f"{API}/state"
ITEMS = f"{API}/items"
ACTIONS = f"{API}/actions"
CHAT = f"{API}/chat"
PROPOSALS = f"{API}/proposals"
EXPORT = f"{API}/export"
FLOORPLAN = f"{API}/floorplan"
FLOORPLAN_IMAGE = f"{FLOORPLAN}/image"
LOCATIONS = f"{API}/locations"
FLOORPLAN_BODY_LIMIT = 16 * 1024 * 1024
CATEGORIES = {"food": "食材", "supplies": "家用消耗品", "maintenance": "維護用品", "tools": "工具"}
USERFIELDS = {"category": "分類", "model": "型號", "interval_days": "維護間隔天數", "last_maintained": "上次維護"}
Nonnegative = Annotated[float, Field(ge=0, le=1e9, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=1e9, allow_inf_nan=False)]
Category = Literal["food", "supplies", "maintenance", "tools"]
Text = Annotated[str, Field(min_length=1, max_length=200)]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class ItemCreate(StrictModel):
    name: Text
    category: Category
    quantity: Nonnegative = 0
    unit: Text = "個"
    location: Text = "未分類"
    min_quantity: Nonnegative = 0
    best_before: date | None = None
    opened: bool = False
    model: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=4000)
    interval_days: int | None = Field(default=None, ge=1, le=36500)
    last_maintained: date | None = None

class ItemPatch(StrictModel):
    name: Text | None = None
    category: Category | None = None
    quantity: Nonnegative | None = None
    unit: Text | None = None
    location: Text | None = None
    min_quantity: Nonnegative | None = None
    best_before: date | None = None
    opened: bool | None = None
    model: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    interval_days: int | None = Field(default=None, ge=1, le=36500)
    last_maintained: date | None = None

class Action(StrictModel):
    type: Literal["purchase", "consume", "maintain", "shopping_add", "shopping_complete"]
    item_id: int = Field(gt=0)
    amount: Positive | None = None
    shopping_id: int | None = Field(default=None, gt=0)
    best_before: date | None = None
    opened: bool = False

class PlannedAction(StrictModel):
    type: Literal["purchase", "consume", "maintain", "shopping_add", "shopping_complete", "create_item", "update_item"]
    item_id: int | None = Field(default=None, gt=0)
    amount: Positive | None = None
    shopping_id: int | None = Field(default=None, gt=0)
    best_before: date | None = None
    opened: bool = False
    item: ItemCreate | None = None
    patch: ItemPatch | None = None
    amount_source: str | None = Field(default=None, max_length=80, description="Exact quantity phrase in latest user message; never guess")

class AIResponse(StrictModel):
    reply: str = Field(min_length=1, max_length=12000)
    summary: str = Field(default="", max_length=1200)
    actions: list[PlannedAction] = Field(default_factory=list, max_length=10)

class Turn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)

class ChatInput(StrictModel):
    message: str = Field(min_length=1, max_length=6000)
    history: list[Turn] = Field(default_factory=list, max_length=20)

class LoginInput(StrictModel):
    password: str = Field(min_length=1, max_length=1024)


Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
GeometryID = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]

class Point(StrictModel):
    x: Coordinate
    y: Coordinate

class Canvas(StrictModel):
    width: int = Field(ge=200, le=8000, strict=True)
    height: int = Field(ge=200, le=8000, strict=True)

    @model_validator(mode="after")
    def pixel_limit(self):
        if self.width * self.height > 16_000_000:
            raise ValueError("Canvas exceeds 16 megapixels")
        return self

class Room(StrictModel):
    id: GeometryID
    name: Text
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    points: list[Point] = Field(min_length=3, max_length=40)

    @model_validator(mode="after")
    def nonempty_polygon(self):
        points = self.points
        area = sum(p.x * q.y - q.x * p.y for p, q in zip(points, points[1:] + points[:1]))
        if abs(area) < 1e-10:
            raise ValueError("Room must have a nonzero area")
        return self

class Spot(Point):
    id: GeometryID
    location_id: int = Field(gt=0, strict=True)
    room_id: GeometryID | None = None

class SavePlan(StrictModel):
    revision: int = Field(ge=0, strict=True)
    title: Text
    canvas: Canvas
    rooms: list[Room] = Field(max_length=60)
    spots: list[Spot] = Field(max_length=300)
    image_data_url: str | None = Field(default=None, max_length=14_000_000)

    @model_validator(mode="after")
    def unique_references(self):
        room_ids = [room.id for room in self.rooms]
        spot_ids = [spot.id for spot in self.spots]
        location_ids = [spot.location_id for spot in self.spots]
        if len(set(room_ids + spot_ids)) != len(room_ids + spot_ids):
            raise ValueError("Room and spot IDs must be unique")
        if len(set(location_ids)) != len(location_ids):
            raise ValueError("Each location can only be pinned once")
        if any(spot.room_id is not None and spot.room_id not in room_ids for spot in self.spots):
            raise ValueError("Spot references an unknown room")
        return self

class LocationName(StrictModel):
    name: Text
