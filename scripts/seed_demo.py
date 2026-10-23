#!/usr/bin/env python3
"""Explicit TEST fixture for one isolated demo household, never real household inventory.

Usage: uv run python scripts/seed_demo.py --confirm-demo
Setup alone leaves a fresh household empty. Every demo product is labeled 示範.
Re-running keeps existing demo quantities and does not duplicate products.
To remove these TEST products: add --clear-demo --confirm-demo (keeps volumes).
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.setup_grocy import read_env  # noqa: E402
from server.contracts import ItemCreate  # noqa: E402
from server.grocy import Grocy  # noqa: E402

PREFIX = '示範｜'
DEMO_DESCRIPTION = '測試示範資料：不是你的真實家庭庫存。'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-demo', action='store_true', help='Explicitly initialize TEST data in this local demo household')
    parser.add_argument('--clear-demo', action='store_true', help='Remove only clearly labeled TEST products; requires --confirm-demo')
    args = parser.parse_args()
    if not args.confirm_demo:
        parser.error('Only TEST data: pass --confirm-demo to initialize the isolated demo household')
    env = read_env()
    if env.get('GROCY_URL') != 'http://127.0.0.1:9283':
        parser.error('Demo seeding is restricted to the isolated loopback Grocy')
    grocy = Grocy(env['GROCY_URL'], env['GROCY_API_KEY'])
    products = grocy.objects('products')
    if any(not product['name'].startswith(PREFIX) for product in products):
        parser.error('Refusing to mix TEST fixtures with existing non-demo household inventory')
    if args.clear_demo:
        if any(product.get('description') != DEMO_DESCRIPTION for product in products):
            parser.error('Refusing to delete products without the exact TEST fixture marker')
        for product in products:
            grocy.request('DELETE', f"objects/products/{int(product['id'])}")
        remaining = grocy.objects('products')
        if remaining:
            raise RuntimeError('Demo cleanup incomplete; no additional deletion attempted')
        print(f'Cleared {len(products)} TEST demo products; household inventory is empty. Volumes and configuration retained.')
        return
    today = datetime.now(ZoneInfo('Asia/Taipei')).date()
    fixtures = [
        dict(name='鮮奶', category='food', quantity=1, unit='瓶', location='冰箱', min_quantity=2,
             best_before=(today + timedelta(days=2)).isoformat(), opened=True),
        dict(name='雞蛋', category='food', quantity=6, unit='顆', location='冰箱', min_quantity=4,
             best_before=(today + timedelta(days=7)).isoformat()),
        dict(name='義大利麵', category='food', quantity=0, unit='包', location='食品櫃', min_quantity=1),
        dict(name='洗碗精', category='supplies', quantity=0.5, unit='瓶', location='廚房', min_quantity=1, opened=True),
        dict(name='衛生紙', category='supplies', quantity=4, unit='包', location='儲藏櫃', min_quantity=3),
        dict(name='空氣清淨機濾網', category='maintenance', quantity=1, unit='台', location='客廳',
             model='DEMO-AIR-01', interval_days=90, last_maintained=(today - timedelta(days=91)).isoformat()),
        dict(name='冷氣清潔', category='maintenance', quantity=1, unit='台', location='臥室',
             model='DEMO-AC-01', interval_days=180, last_maintained=(today - timedelta(days=160)).isoformat()),
        dict(name='十字螺絲起子', category='tools', quantity=1, unit='支', location='工具箱', model='DEMO-PH2'),
    ]
    existing = {p['name'] for p in products}
    created = 0
    for fixture in fixtures:
        fixture['name'] = PREFIX + fixture['name']
        if fixture['name'] not in existing:
            fixture['description'] = DEMO_DESCRIPTION
            data = ItemCreate(**fixture).model_dump(mode='json')
            grocy.create(data)
            created += 1
    state = grocy.state()
    print(f'TEST demo household: created {created}; total {len(state["items"])} clearly labeled demo products; '
          f'{len(set(item["category"] for item in state["items"]))} categories. No real household data.')


if __name__ == '__main__':
    main()
