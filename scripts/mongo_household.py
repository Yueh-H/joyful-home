"""Offline JSON DTO validation/import/export for the local household aggregate."""
import argparse
import ast
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import types

from pymongo.errors import DuplicateKeyError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.mongo_inventory import MongoInventory, numeric_id, validated_snapshot


def validate_layout(layout, schema_file=None):
    """Use the companion's real schema without importing its app or any DB store."""
    path = Path(schema_file) if schema_file else ROOT.parent / 'joyful-home-map/server.py'
    tree = ast.parse(path.read_text())
    constants = {'Coord','Positive','Angle','GeometryID','Text','Color','Percent','PositivePercent'}
    classes = {'StrictModel','Point','Canvas','Room','Wall','Opening','Compartment','StorageView','Furniture','Layout'}
    body = [ast.Import(names=[ast.alias(name='math')])]
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in {'typing','pydantic'}:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in classes:
            body.append(node)
    module = types.ModuleType('_mongo_import_layout_contract')
    sys.modules[module.__name__] = module
    exec(compile(ast.fix_missing_locations(ast.Module(body=body,type_ignores=[])), str(path), 'exec'), module.__dict__)
    module.Layout.model_validate(layout)
    return copy.deepcopy(layout)


def canonical(snapshot, schema_file=None):
    state = {key: snapshot['state'][key] for key in ('items','shopping')}
    locations = snapshot['locations']
    if isinstance(locations, dict):
        locations = locations['locations']
    inventory = validated_snapshot(state, locations)
    supplied_counters = snapshot.get('counters')
    if supplied_counters is not None:
        if not isinstance(supplied_counters, dict) or set(supplied_counters) != {'items','locations','shopping'}:
            raise ValueError('Exported counters are incomplete.')
        for kind, value in supplied_counters.items():
            value = numeric_id(value)
            if value < inventory['counters'][kind]:
                raise ValueError('Exported next-ID counter would reuse an existing ID.')
            inventory['counters'][kind] = value
    layout = validate_layout(snapshot['layout'], schema_file)
    known = {row['id'] for row in locations}
    bound = [row for obj in layout['objects'] for row in [obj, *obj.get('storage', {}).get('compartments', [])]]
    if any(row.get('location_id') is not None and row['location_id'] not in known for row in bound):
        raise ValueError('Layout contains an unknown inventory location ID.')
    dto = {'schema_version': 1, 'state': {'items': sorted(state['items'],key=lambda r:r['id']),
                                        'shopping': sorted(state['shopping'],key=lambda r:r['id'])},
           'locations': sorted(locations,key=lambda r:r['id']), 'layout': layout}
    if supplied_counters is not None:
        dto['counters'] = copy.deepcopy(inventory['counters'])
    digest = hashlib.sha256(json.dumps(dto,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return dto, inventory, digest


def counts(dto):
    return {'items':len(dto['state']['items']),'shopping':len(dto['state']['shopping']),
            'locations':len(dto['locations']),'objects':len(dto['layout']['objects']),
            'compartments':sum(len(o.get('storage',{}).get('compartments',[])) for o in dto['layout']['objects']),
            'revision':dto['layout']['revision']}


def prepare(snapshot, home_id='home', schema_file=None, expected=None):
    dto, inventory, digest = canonical(snapshot,schema_file)
    actual = counts(dto)
    if expected and any(actual.get(key)!=value for key,value in expected.items()):
        raise ValueError('Snapshot counts/revision do not match the approved migration baseline.')
    document = {'_id':home_id,'schema_version':1,'version':0,'inventory':inventory,'layout':dto['layout'],
                'import':{'sha256':digest,'at':datetime.now(timezone.utc).isoformat(),'source':'validated-json-dto'}}
    return document, actual


def import_snapshot(adapter, snapshot, schema_file=None, expected=None):
    document, actual = prepare(snapshot,adapter.home_id,schema_file,expected)
    current = adapter.collection.find_one({'_id':adapter.home_id})
    if current is not None:
        if (current.get('import') or {}).get('sha256') != document['import']['sha256']:
            raise ValueError('Destination contains a different household snapshot; import refused.')
        # An import marker alone never authorizes overwriting later real edits.
        if current.get('inventory')!=document['inventory'] or current.get('layout')!=document['layout']:
            raise ValueError('Household changed after import; existing data preserved.')
        return {**actual,'sha256':document['import']['sha256'],'already_imported':True}
    if adapter.collection.find_one({}, {'_id':1}) is not None:
        raise ValueError('Destination database already has a household; import refused.')
    if any(adapter.client[adapter.collection.database.name][name].estimated_document_count() for name in adapter.client[adapter.collection.database.name].list_collection_names()):
        raise ValueError('Destination database is not empty; import refused.')
    try:
        adapter.collection.insert_one(document)
    except DuplicateKeyError:
        raise ValueError('Destination was initialized concurrently; inspect it before retrying.') from None
    saved = adapter.document()
    if saved!=document:
        raise ValueError('Mongo import readback differs; destination preserved for inspection.')
    return {**actual,'sha256':document['import']['sha256'],'already_imported':False}


def export_snapshot(adapter):
    document = adapter.document()
    data = document['inventory']
    # Keep batch detail and stable integer IDs for subsequent local/cloud migration.
    items = copy.deepcopy(list(data['items'].values()))
    for item in items:
        item['location'] = data['locations'][str(item['location_id'])]['name']
    return {'schema_version':1,'state':{'items':sorted(items,key=lambda r:r['id']),
                                      'shopping':sorted(data['shopping'].values(),key=lambda r:r['id'])},
            'locations':sorted(data['locations'].values(),key=lambda r:r['id']),
            'counters':copy.deepcopy(data['counters']),
            'layout':copy.deepcopy(document['layout'])}


def write_private(path, data):
    path=Path(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with open(path,'x',opener=lambda p,flags:os.open(p,flags,0o600)) as handle:
        json.dump(data,handle,ensure_ascii=False,indent=2,allow_nan=False)
        handle.write('\n')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['validate','prepare','import','export'])
    p.add_argument('--snapshot',type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--layout-schema',type=Path)
    p.add_argument('--uri',default=os.getenv('MONGODB_URI','mongodb://mongo:27017'))
    p.add_argument('--database',default=os.getenv('MONGODB_DATABASE','joyful_home'))
    p.add_argument('--home-id',default=os.getenv('HOME_ID','home'))
    p.add_argument('--approved-home',action='store_true',help='Require revision37,97 locations,35 objects,76 slots,empty inventory/shopping.')
    args=p.parse_args()
    expected={'items':0,'shopping':0,'locations':97,'objects':35,'compartments':76,'revision':37} if args.approved_home else None
    if args.operation in {'validate','prepare','import'}:
        if not args.snapshot:p.error('--snapshot is required')
        snapshot=json.loads(args.snapshot.read_text())
    if args.operation in {'validate','prepare'}:
        document, actual=prepare(snapshot,args.home_id,args.layout_schema,expected)
        if args.operation=='prepare':
            if not args.output:p.error('--output is required')
            write_private(args.output,document)
        print(json.dumps({**actual,'sha256':document['import']['sha256'],'database_written':False}))
    else:
        adapter=MongoInventory(uri=args.uri,database=args.database,home_id=args.home_id)
        try:
            if args.operation=='import':
                print(json.dumps(import_snapshot(adapter,snapshot,args.layout_schema,expected)))
            else:
                if not args.output:p.error('--output is required')
                snapshot=export_snapshot(adapter)
                write_private(args.output,snapshot)
                print(json.dumps({**counts(snapshot),'exported':True}))
        finally:adapter.client.close()


if __name__=='__main__':
    main()
