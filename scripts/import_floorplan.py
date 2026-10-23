#!/usr/bin/env python3
"""Import a private floorplan into the local running household service.

The image and layout remain runtime data; do not add them to Git.
"""
import argparse
import base64
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import contracts as c


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--layout', type=Path, required=True,
                        help='Private JSON containing title, canvas, rooms and spots')
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--replace', action='store_true',
                        help='Explicitly replace a layout that has already been edited')
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
        parser.error('Private imports are restricted to the local household service.')
    raw = args.image.read_bytes()
    if len(raw) > 10 * 1024 * 1024:
        parser.error('The image must be 10 MiB or smaller.')
    mime = mimetypes.guess_type(args.image.name)[0]
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        parser.error('Choose a PNG, JPEG or WebP image.')
    layout = json.loads(args.layout.read_text())
    password = dotenv_values(ROOT / '.env').get('HOME_PASSWORD')
    if not password:
        parser.error('The local HOME_PASSWORD is not configured.')
    origin = args.url.rstrip('/')
    with httpx.Client(base_url=origin, headers={'Origin': origin}, timeout=60) as client:
        login = client.post(c.LOGIN, json={'password': password})
        if login.status_code != 200:
            raise RuntimeError(f'Local login failed (HTTP {login.status_code}).')
        try:
            response = client.get(c.FLOORPLAN)
            response.raise_for_status()
            current = response.json()
            if (current['rooms'] or current['spots'] or current['image_url']) and not args.replace:
                parser.error('A layout already exists; use the editor or explicitly pass --replace.')
            payload = {key: layout[key] for key in ('title', 'canvas', 'rooms', 'spots')}
            payload['revision'] = current['revision']
            payload['image_data_url'] = f'data:{mime};base64,' + base64.b64encode(raw).decode('ascii')
            saved = client.put(c.FLOORPLAN, json=payload)
            if saved.status_code != 200:
                raise RuntimeError(f'Import failed (HTTP {saved.status_code}); existing layout retained.')
            plan = saved.json()
            print(f"Imported private floorplan: revision {plan['revision']}, "
                  f"{len(plan['rooms'])} rooms, {len(plan['spots'])} storage markers.")
        finally:
            client.post(c.LOGOUT, json={})


if __name__ == '__main__':
    main()
