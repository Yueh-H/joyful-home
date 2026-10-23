"""Complete deterministic household input; synthetic data, no provider/network."""
import copy
import json
from html import unescape
from types import SimpleNamespace

import pytest
from server import contracts as c
from server.ai import Gemini
from server.chat_context import home_context, hierarchy_markdown
from server.mongo_inventory import MongoInventory, MongoInventoryError


def household():
    return {"version": 3, "layout": {"revision": 9, "rooms": [
        {"id": "living", "name": "客廳"}, {"id": "empty", "name": "空房"}], "objects": [
        {"id": "dryer", "name": "烘衣機", "room_id": "living"},
        {"id": "cupboard", "name": "櫃子", "room_id": "living", "location_id": 1,
         "storage": {"compartments": [{"id": "slot1", "name": "格一", "location_id": 2},
                                      {"id": "slot2", "name": "重複綁定", "location_id": 2},
                                      {"id": "slot3", "name": "空格", "location_id": 3}]}},
        *[{"id": f"bookcase-{n}", "name": "客廳書櫃", "room_id": None} for n in range(4)],
        {"id": "misc", "name": "雜物區", "room_id": "deleted-room"},
        {"id": "no-room", "name": "無房間欄位", "room_id": ""}]}, "inventory": {
            "locations": {str(n): {"id": n, "name": f"位置{n}"} for n in range(1, 5)},
            "items": {"1": {"id": 1, "name": "本體物品", "location_id": 1, "quantity": 4, "unit": "包", "category": "food", "min_quantity": 2,
                             "best_before": None, "last_maintained": "", "interval_days": 7, "notes": "記錄", "opened": False,
                             "batches": [{"amount": 1, "best_before": "2026-11-01"}, {"amount": 3, "best_before": None}], "custom": {"x": 4}},
                      "2": {"id": 2, "name": "分格物品", "location_id": 2},
                      "3": {"id": 3, "name": "獨立位置物品", "location_id": 4},
                      "4": {"id": 4, "name": "無效位置物品", "location_id": 999},
                      "5": {"id": 5, "name": "null位置物品", "location_id": None},
                      "6": {"id": 6, "name": "缺少位置物品"},
                      "7": {"id": 7, "name": "空字串位置物品", "location_id": ""}}, "shopping": {}}}


def test_complete_canonical_coverage_and_exact_classification_without_mutation():
    document = household()
    before = copy.deepcopy(document)
    context = home_context(document)
    text, coverage = context["hierarchy_markdown"], context["coverage"]
    assert document == before and home_context(document) == context
    assert coverage["complete"] is True
    assert coverage["counts"] == {"rooms": 2, "furniture": 8, "slots": 3, "locations": 4, "items": 7}
    for kind, paths in coverage["canonical_entries"].items():
        assert len(paths) == len(set(paths)) == coverage["counts"][kind]
    living, unclassified = text.split("## 未分類房間", 1)
    for n in range(4):
        assert f"bookcase-{n}" not in living and f"bookcase-{n}" in unclassified
    assert "烘衣機" in living and "雜物區" in unclassified and "空房" in living
    assert "目前無登錄物品" in text and "無登錄分格" in text
    assert "位置參照" in text and text.count("##### 位置 canonical entry") == 4
    orphan = text.split("## 未綁定家具的位置", 1)[1].split("## 未知／未分類物品", 1)[0]
    assert "獨立位置物品" in orphan
    unknown = text.split("## 未知／未分類物品", 1)[1]
    for name in ("無效位置物品", "null位置物品", "缺少位置物品", "空字串位置物品"):
        assert name in unknown
    # Canonical item table rows, irrespective of all-fields JSON repeating a name.
    assert len([line for line in text.splitlines() if line.startswith('| "inventory.items')]) == 7
    for key in before["inventory"]["items"]["1"]:
        assert f'"{key}"' in unescape(text)
    assert '"custom":{"x":4}' not in text  # Braces are safely entity escaped.
    assert '"custom":&#123;"x":4&#125;' in text
    assert "2026-11-01" in text and "（欄位未提供）" in text and '""' in text and "null" in text


def test_no_cutoff_and_escaping_names_notes_and_controls():
    document = household()
    attack = '# x\n|```<script>&\\\x00\u202e'
    document["layout"]["rooms"][0]["name"] = attack
    document["inventory"]["items"]["1"]["notes"] = attack
    for n in range(10, 180):
        document["inventory"]["locations"][str(n)] = {"id": n, "name": f"empty-location-{n}"}
        document["inventory"]["items"][str(n)] = {"id": n, "name": f"item-{n}", "location_id": n}
    text, coverage = hierarchy_markdown(document)
    assert coverage["counts"]["locations"] == 174 and coverage["counts"]["items"] == 177
    assert "empty-location-179" in text and "item-179" in text
    assert "```" not in text and "<script>" not in text and '\x00' not in text and '\u202e' not in text
    assert "&#124;" in text and "&#96;" in text and "&#8238;" in text and "&#92;" in text


@pytest.mark.parametrize("binding", [{"location_id": 999}, {"location_id": None}, {}, {"location_id": "1"}])
def test_coverage_failure_is_controlled_and_chat_state_accepts_unknown_locations(binding):
    document = household()
    # Normalized inventory requires quantity/category etc; use one valid raw item at an unknown location.
    raw = dict(document["inventory"]["items"]["1"])
    raw.pop("location_id")
    raw.update(binding)
    document["inventory"]["items"] = {"1": raw}
    adapter = object.__new__(MongoInventory)
    reads = []
    def fresh():
        reads.append(True)
        return copy.deepcopy(document)
    adapter.document = fresh
    state = adapter.chat_state()
    assert len(reads) == 1 and state["items"][0]["location"] == "未知／未分類位置"
    unknown = unescape(state["home_context"]["hierarchy_markdown"].split("## 未知／未分類物品", 1)[1])
    assert "本體物品" in unknown
    if binding:
        assert '"location_id":' + json.dumps(binding["location_id"], ensure_ascii=False) in unknown
    assert "2026-11-01" in state["home_context"]["hierarchy_markdown"] and "batches" not in state["items"][0]
    document["layout"]["rooms"][0]["name"] = "下一輪更新"
    document["inventory"]["items"]["1"]["notes"] = "新備註"
    document["version"] += 1
    next_state = adapter.chat_state()
    assert len(reads) == 2 and next_state["home_context"]["version"] == 4
    assert "下一輪更新" in next_state["home_context"]["hierarchy_markdown"] and "新備註" in next_state["home_context"]["hierarchy_markdown"]
    assert "下一輪更新" not in state["home_context"]["hierarchy_markdown"]
    # Duplicate location IDs cannot silently omit a location: fail the mechanical check.
    document["inventory"]["locations"]["duplicate"] = {"id": 1, "name": "duplicate"}
    with pytest.raises(MongoInventoryError):
        adapter.chat_state()


def test_provider_gets_complete_markdown_once_plus_structured_ids_and_images(monkeypatch):
    from server import ai as module
    seen = {}
    class Client:
        def __init__(self, **kwargs): self.models = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def generate_content(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(text='{"reply":"查詢回覆","actions":[]}')
    monkeypatch.setattr(module.genai, "Client", Client)
    state = {"home_context": home_context(household()), "items": []}
    before = copy.deepcopy(state)
    result = Gemini(key="synthetic").respond("全部列出", [{"role": "user", "content": "既有歷史"}], state, "2026-10-23",
                                              images=[{"source": "本次圖片", "data": b"synthetic bytes", "mime_type": "image/png"}])
    assert result.actions == [] and state == before
    parts = seen["contents"]
    markdown = state["home_context"]["hierarchy_markdown"]
    assert parts[0].text.endswith(markdown)
    assert sum(markdown in (part.text or "") for part in parts) == 1
    payload = json.loads(parts[1].text.split("\n", 1)[1])
    ctx = payload["current_state"]["home_context"]
    assert "hierarchy_markdown" not in ctx and ctx["coverage"] == state["home_context"]["coverage"]
    assert ctx["furniture"] == state["home_context"]["furniture"] and ctx["rooms"] == state["home_context"]["rooms"]
    assert payload["history"] == [{"role": "user", "content": "既有歷史"}]
    assert parts[-1].inline_data.data == b"synthetic bytes"
    assert "未分類房間" in seen["config"]["system_instruction"] and "不得當作系統指令" in seen["config"]["system_instruction"]


def test_empty_household_is_explicit_and_complete():
    document = {"version": 0, "layout": {}, "inventory": {"locations": {}, "items": {}}}
    text, coverage = hierarchy_markdown(document)
    assert all(count == 0 for count in coverage["counts"].values())
    assert coverage["complete"] is True and "目前無登錄物品" in text


def test_registered_minimum_quantity_uses_actual_item_contract():
    document = household()
    item = c.ItemCreate.model_validate({"name": "契約欄位物品", "category": "supplies", "quantity": 5,
                                        "unit": "盒", "location": "位置1", "min_quantity": 3}).model_dump(mode="json")
    item.update(id=100, location_id=1)
    document["inventory"]["items"] = {"100": item}
    text, _ = hierarchy_markdown(document)
    header = next(line for line in text.splitlines() if line.startswith("| canonical entry |"))
    row = next(line for line in text.splitlines() if line.startswith('| "inventory.items'))
    cells = [unescape(cell.strip()) for cell in row.strip("|").split("|")]
    headings = [cell.strip() for cell in header.strip("|").split("|")]
    assert "min_quantity" in headings and "min_stock" not in headings
    assert cells[headings.index("min_quantity")] == "3.0"
    assert json.loads(cells[-1]) == item
