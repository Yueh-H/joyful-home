"""Read-only spatial projection of one canonical household document; no model/photo assets."""
import copy


def home_context(document):
    layout, inventory = document["layout"], document["inventory"]
    item_ids = {}
    for item in inventory["items"].values():
        item_ids.setdefault(item.get("location_id"), []).append(item["id"])
    furniture_fields = ("id", "name", "kind", "room_id", "x", "y", "width", "height", "rotation", "location_id", "notes", "storage")
    return {"available": True, "version": document["version"], "layout_revision": layout.get("revision"),
            "coordinate_system": "x/y為未旋轉家具左上角畫布座標，rotation為繞中心旋轉的度數；長度乘canvas.mm_per_unit得mm。分格座標為家具前視圖0–100百分比。未綁定location_id或room_id表示未知，不可捏造。",
            "canvas": copy.deepcopy(layout.get("canvas", {})),
            "rooms": [{key: copy.deepcopy(room[key]) for key in ("id", "name", "points", "label") if key in room} for room in layout.get("rooms", [])],
            "furniture": [{key: copy.deepcopy(obj[key]) for key in furniture_fields if key in obj} for obj in layout.get("objects", [])],
            "locations": [dict(copy.deepcopy(location), item_ids=item_ids.get(location["id"], []))
                          for location in sorted(inventory["locations"].values(), key=lambda row: row["id"])]}
