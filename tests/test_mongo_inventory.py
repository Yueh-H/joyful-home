"""Real isolated Mongo tests. No household records, credentials, or cloud access."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
import uuid

import pytest
from pymongo import MongoClient
from server.mongo_inventory import MongoInventory, MongoInventoryError
from scripts.mongo_household import import_snapshot, export_snapshot, prepare


@pytest.fixture(scope='module')
def mongo_server(tmp_path_factory):
    binary = shutil.which('mongod')
    if binary is None:
        pytest.skip('Real local Mongo test requires mongod; cloud is never used.')
    directory=tmp_path_factory.mktemp('isolated-mongo')
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    uri=f'mongodb://127.0.0.1:{port}'
    process=None
    def start():
        nonlocal process
        process=subprocess.Popen([binary,'--dbpath',str(directory),'--bind_ip','127.0.0.1','--port',str(port),'--nounixsocket','--logpath',str(directory/'mongod.log'),'--quiet'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        with MongoClient(uri,serverSelectionTimeoutMS=100) as client:
            for _ in range(60):
                if process.poll() is not None:raise RuntimeError('Isolated mongod failed to start; see private test log.')
                try:client.admin.command('ping');return
                except Exception:time.sleep(.1)
        raise RuntimeError('Isolated mongod startup timed out.')
    def stop():
        process.terminate();process.wait(timeout=20)
    start()
    yield uri,stop,start
    if process.poll() is None:stop()


def sample():
    return {'state':{'items':[],'shopping':[]},'locations':[{'id':97,'name':'測試儲位'}],
            'layout':{'revision':37,'name':'Synthetic test','canvas':{'width':1200,'height':900,'mm_per_unit':10},'rooms':[],'walls':[],'openings':[],'objects':[]}}


@pytest.fixture
def adapter(mongo_server):
    uri,_,_=mongo_server
    name='test_'+uuid.uuid4().hex
    a=MongoInventory(uri=uri,database=name,retries=20)
    import_snapshot(a,sample())
    yield a
    a.client.drop_database(name);a.client.close()


def create(a,quantity=3):
    return a.create({'name':'驗證蛋','category':'food','quantity':quantity,'location':'測試儲位','unit':'顆'})


def test_missing_household_is_error_and_read_does_not_seed(mongo_server):
    a=MongoInventory(uri=mongo_server[0],database='empty_'+uuid.uuid4().hex)
    with pytest.raises(MongoInventoryError,match='尚未匯入'):a.state()
    assert a.collection.find_one({}) is None
    a.client.close()


def test_import_ids_counters_idempotency_and_reject_overwrite(adapter):
    assert adapter.locations()==[{'id':97,'name':'測試儲位'}]
    assert import_snapshot(adapter,sample())['already_imported'] is True
    assert adapter.create_location('新測試位置')['id']==98
    with pytest.raises(ValueError,match='changed after import'):import_snapshot(adapter,sample())
    different=sample();different['layout']['name']='別的快照'
    with pytest.raises(ValueError,match='different household'):import_snapshot(adapter,different)


def test_two_clients_cannot_overconsume(adapter):
    iid=create(adapter,1)
    other=MongoInventory(uri=mongo_server_uri(adapter),database=adapter.collection.database.name)
    barrier=threading.Barrier(2)
    def consume_with(a):
        barrier.wait()
        try:a.action({'type':'consume','item_id':iid,'amount':1},'2026-10-23');return 'ok'
        except MongoInventoryError as e:return e.status_code
        except Exception as e:
            if getattr(e,'status_code',None)==409:return 409
            raise
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(consume_with,[adapter,other]))
    assert sorted(results,key=str)==sorted(['ok',409],key=str)
    assert adapter.state()['items'][0]['quantity']==0
    other.client.close()


def mongo_server_uri(a):
    host,port=a.client.address
    return f'mongodb://{host}:{port}'


def test_shared_version_cas_preserves_concurrent_layout_write(adapter):
    iid=create(adapter)
    original=adapter.collection
    class Race:
        raced=False
        def find_one(self,*args,**kwargs):return original.find_one(*args,**kwargs)
        def update_one(self,query,change):
            if not self.raced:
                self.raced=True
                assert original.update_one({'_id':'home','version':query['version']},{'$set':{'layout.name':'Concurrent map'},'$inc':{'version':1}}).matched_count==1
            return original.update_one(query,change)
    adapter.collection=Race()
    adapter.action({'type':'consume','item_id':iid,'amount':1},'2026-10-23')
    document=original.find_one({'_id':'home'})
    assert document['layout']['name']=='Concurrent map'
    assert document['inventory']['items'][str(iid)]['quantity']==2


def test_invalid_shopping_operation_has_no_partial_purchase(adapter):
    iid=create(adapter)
    before=adapter.document()
    with pytest.raises(Exception):adapter.action({'type':'shopping_complete','item_id':iid,'shopping_id':4,'amount':2},'2026-10-23')
    assert adapter.document()==before
    adapter.action({'type':'shopping_add','item_id':iid,'amount':2},'2026-10-23')
    sid=adapter.state()['shopping'][0]['id']
    adapter.action({'type':'shopping_complete','item_id':iid,'shopping_id':sid,'amount':2,'best_before':'2026-12-01','opened':True},'2026-10-23')
    assert adapter.state()['items'][0]['quantity']==5 and adapter.state()['shopping']==[]


def test_batches_maintenance_move_and_guarded_delete(adapter):
    iid=create(adapter,1)
    adapter.patch(iid,{'best_before':'2026-11-01'},adapter.state()['items'][0])
    adapter.action({'type':'purchase','item_id':iid,'amount':2,'best_before':'2026-12-01','opened':True},'2026-10-23')
    state=adapter.state()['items'][0]
    assert state['best_before']=='2026-11-01' and state['opened'] is False
    adapter.patch(iid,{'location':'另一位置','interval_days':7},state)
    adapter.action({'type':'maintain','item_id':iid},'2026-10-23')
    item=adapter.state()['items'][0]
    assert item['next_due']=='2026-10-30' and item['location']=='另一位置'
    with pytest.raises(Exception):adapter.delete_item(iid,confirm_name='錯名稱',expected_location_id=item['location_id'])
    assert adapter.delete_item(iid,confirm_name=item['name'],expected_location_id=item['location_id'])=={'ok':True,'deleted_ids':[iid]}
    assert adapter.state()['items']==[]


def test_export_roundtrip_preserves_batch_detail_and_layout(adapter):
    iid=create(adapter,1)
    adapter.action({'type':'purchase','item_id':iid,'amount':2,'best_before':'2026-12-01','opened':True},'2026-10-23')
    exported=export_snapshot(adapter)
    assert len(exported['state']['items'][0]['batches'])==2
    document,_=prepare(exported)
    assert document['inventory']==adapter.document()['inventory']
    assert document['layout']==adapter.document()['layout']


def test_real_mongod_restart_persists_journaled_document(adapter,mongo_server):
    iid=create(adapter,2)
    before=adapter.document()
    assert adapter.collection.write_concern.document=={'j':True,'w':1}
    _,stop,start=mongo_server
    stop();start()
    assert adapter.document()==before
    assert adapter.state()['items'][0]['id']==iid

def test_export_does_not_reuse_a_deleted_highest_id(adapter):
    iid=create(adapter,0)
    adapter.delete_item(iid,require_empty=True)
    exported=export_snapshot(adapter)
    assert exported['counters']['items']==iid+1
    document,_=prepare(exported)
    assert document['inventory']['counters']['items']==iid+1

def test_private_export_writes_real_file_600_and_refuses_overwrite(tmp_path):
    from scripts.mongo_household import write_private
    target=tmp_path/'private-export.json'
    snapshot={'state':{'items':[],'shopping':[]},'note':'私人備份'}
    write_private(target,snapshot)
    assert json.loads(target.read_text())==snapshot
    assert target.stat().st_mode & 0o777 == 0o600
    original=target.read_bytes()
    with pytest.raises(FileExistsError):
        write_private(target,{'overwritten':True})
    assert target.read_bytes()==original
