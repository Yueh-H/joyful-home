"""Independent authoritative-inventory checks; all documents and transactions synthetic."""
import copy
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from server.firestore_inventory import FirestoreInventory, FirestoreInventoryError


class Snapshot:
    def __init__(self,path,data):
        self.id=path.rsplit('/',1)[-1]
        self.exists=data is not None
        self.data=copy.deepcopy(data)
    def to_dict(self):
        return copy.deepcopy(self.data)


class Reference:
    def __init__(self,client,path):
        self.client,self.path=client,path
    def collection(self,name):
        return Collection(self.client,self.path+'/'+name)
    def get(self,transaction=None):
        self.client.read(transaction)
        return Snapshot(self.path,self.client.data.get(self.path))


class Collection:
    def __init__(self,client,path):
        self.client,self.path=client,path
    def document(self,value):
        return Reference(self.client,self.path+'/'+value)
    def stream(self,transaction=None):
        self.client.read(transaction)
        prefix=self.path+'/'
        return [Snapshot(p,d) for p,d in self.client.data.items() if p.startswith(prefix) and '/' not in p[len(prefix):]]


class Transaction:
    def __init__(self):
        self.pending={}
        self.written=False
    def set(self,ref,data):
        self.written=True
        self.pending[ref.path]=copy.deepcopy(data)
    def delete(self,ref):
        self.written=True
        self.pending[ref.path]=None


class Cloud:
    def __init__(self):
        self.data={}
        self.lock=threading.RLock()
        self.fail_read=self.fail_commit=False
    def collection(self,name):
        return Collection(self,name)
    def transaction(self):
        return Transaction()
    def read(self,tx):
        if self.fail_read:
            raise RuntimeError('private cloud connection details')
        assert not tx or not tx.written,'Firestore does not allow reads after writes'
    def transactional(self,fn):
        def execute(tx):
            with self.lock:
                result=fn(tx)
                if self.fail_commit:
                    raise RuntimeError('private cloud commit details')
                for path,data in tx.pending.items():
                    if data is None:
                        self.data.pop(path,None)
                    else:
                        self.data[path]=data
                return result
        return execute


@pytest.fixture
def inventory():
    cloud=Cloud()
    adapter=FirestoreInventory(client=cloud,transactional=cloud.transactional,household_id='independent_qa')
    return adapter,cloud


def create(adapter,quantity=1,name='Synthetic item',location='Synthetic shelf'):
    return adapter.create({'name':name,'category':'tools','quantity':quantity,'location':location})


def test_independent_cloud_adapter_restart_and_household_isolation(inventory):
    adapter,cloud=inventory
    item_id=create(adapter,quantity=2)
    restarted=FirestoreInventory(client=cloud,transactional=cloud.transactional,household_id='independent_qa')
    assert restarted.state()['items'][0]['id']==item_id
    assert restarted.state()['items'][0]['quantity']==2
    other=FirestoreInventory(client=cloud,transactional=cloud.transactional,household_id='different_qa')
    assert other.state()=={'items':[],'shopping':[]}
    assert all(path.startswith('households/independent_qa/') for path in cloud.data)


def test_independent_concurrent_consume_never_makes_stock_negative(inventory):
    adapter,cloud=inventory
    item_id=create(adapter)
    barrier=threading.Barrier(2)
    def consume(_):
        barrier.wait(timeout=5)
        try:
            adapter.action({'type':'consume','item_id':item_id,'amount':1},'2026-10-23')
            return 200
        except FirestoreInventoryError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(consume,range(2)))==[200,409]
    assert adapter.state()['items'][0]['quantity']==0


def test_independent_shopping_completion_failure_rolls_back_both_records(inventory):
    adapter,cloud=inventory
    item_id=create(adapter,quantity=2)
    adapter.action({'type':'shopping_add','item_id':item_id,'amount':3},'2026-10-23')
    shopping_id=adapter.state()['shopping'][0]['id']
    before=copy.deepcopy(cloud.data)
    cloud.fail_commit=True
    with pytest.raises(FirestoreInventoryError) as failure:
        adapter.action({'type':'shopping_complete','item_id':item_id,'shopping_id':shopping_id,'amount':3},'2026-10-23')
    assert failure.value.status_code==503
    assert 'private cloud' not in str(failure.value)
    assert cloud.data==before
    cloud.fail_commit=False
    adapter.action({'type':'shopping_complete','item_id':item_id,'shopping_id':shopping_id,'amount':3},'2026-10-23')
    state=adapter.state()
    assert state['items'][0]['quantity']==5 and state['shopping']==[]
    with pytest.raises(FirestoreInventoryError) as replay:
        adapter.action({'type':'shopping_complete','item_id':item_id,'shopping_id':shopping_id,'amount':3},'2026-10-23')
    assert replay.value.status_code==409
    assert adapter.state()['items'][0]['quantity']==5


def test_independent_counter_collision_cannot_overwrite_an_item(inventory):
    adapter,cloud=inventory
    create(adapter)
    before=copy.deepcopy(cloud.data)
    cloud.data['households/independent_qa/meta/counters']['items']=0
    with pytest.raises(FirestoreInventoryError) as collision:
        create(adapter,name='Must not overwrite')
    assert collision.value.status_code==409
    assert adapter.state()['items'][0]['name']=='Synthetic item'
    assert len(adapter.state()['items'])==1


def test_independent_stale_quantity_patch_does_not_erase_new_purchase(inventory):
    adapter,cloud=inventory
    item_id=create(adapter,quantity=2)
    stale=adapter.state()['items'][0]
    adapter.action({'type':'purchase','item_id':item_id,'amount':3},'2026-10-23')
    before=copy.deepcopy(cloud.data)
    with pytest.raises(FirestoreInventoryError) as conflict:
        adapter.patch(item_id,{'quantity':1},stale)
    assert conflict.value.status_code==409 and cloud.data==before


def test_independent_guarded_delete_and_linked_shopping_are_atomic(inventory):
    adapter,cloud=inventory
    item_id=create(adapter,quantity=2)
    other_id=create(adapter,name='Other item')
    adapter.action({'type':'shopping_add','item_id':item_id,'amount':2},'2026-10-23')
    adapter.action({'type':'shopping_add','item_id':other_id,'amount':1},'2026-10-23')
    item=next(row for row in adapter.state()['items'] if row['id']==item_id)
    before=copy.deepcopy(cloud.data)
    for name,location in [('Wrong name',item['location_id']),('Synthetic item',item['location_id']+100)]:
        with pytest.raises(FirestoreInventoryError) as denied:
            adapter.delete_item(item_id,confirm_name=name,expected_location_id=location)
        assert denied.value.status_code==409 and cloud.data==before
    cloud.fail_commit=True
    with pytest.raises(FirestoreInventoryError):
        adapter.delete_item(item_id,confirm_name=item['name'],expected_location_id=item['location_id'])
    assert cloud.data==before
    cloud.fail_commit=False
    adapter.delete_item(item_id,confirm_name=item['name'],expected_location_id=item['location_id'])
    state=adapter.state()
    assert [row['id'] for row in state['items']]==[other_id]
    assert all(row['item_id']==other_id for row in state['shopping'])


def test_independent_cloud_read_failure_never_reports_empty_success(inventory):
    adapter,cloud=inventory
    create(adapter)
    cloud.fail_read=True
    for call in (adapter.state,adapter.locations):
        with pytest.raises(FirestoreInventoryError) as failure:
            call()
        assert failure.value.status_code==503 and 'private cloud' not in str(failure.value)


def test_independent_import_refuses_existing_map_without_partial_inventory(inventory):
    adapter,cloud=inventory
    cloud.data['households/independent_qa/maps/layout']={'revision':9,'sentinel':'private existing map'}
    before=copy.deepcopy(cloud.data)
    with pytest.raises(FirestoreInventoryError) as collision:
        adapter.import_snapshot({'items':[],'shopping':[]},[{'id':5,'name':'Synthetic shelf'}])
    assert collision.value.status_code==409 and cloud.data==before


def test_independent_import_keeps_ids_then_allocates_past_them(inventory):
    adapter,cloud=inventory
    source={'items':[{'id':50,'name':'Imported synthetic','category':'tools','quantity':2,'location_id':17}],
            'shopping':[{'id':80,'item_id':50,'amount':1}]}
    adapter.import_snapshot(source,[{'id':17,'name':'Imported shelf'}])
    assert adapter.state()['items'][0]['id']==50
    assert adapter.state()['shopping'][0]['id']==80
    assert create(adapter,name='Next item')==51
    before=copy.deepcopy(cloud.data)
    with pytest.raises(FirestoreInventoryError):
        adapter.import_snapshot(source,[{'id':17,'name':'Imported shelf'}])
    assert cloud.data==before


def test_independent_original_api_reads_cloud_and_never_recurses_for_locations_or_delete(inventory,tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server import contracts as c
    adapter,cloud=inventory
    class NoAI:
        enabled=False
    def forbidden(request):
        raise AssertionError('Firestore inventory must not call Studio or Grocy')
    app=create_app(grocy=adapter,ai=NoAI(),db_path=tmp_path/'session.sqlite3',password='qa-only',secret='q'*40,
                   studio_transport=httpx.MockTransport(forbidden))
    with TestClient(app,headers={'Origin':'http://testserver'}) as client:
        assert client.post(c.LOGIN,json={'password':'qa-only'}).status_code==200
        item_id=create(adapter)
        locations=client.get(c.LOCATIONS)
        assert locations.status_code==200 and len(locations.json()['locations'])==1
        state=client.get(c.STATE)
        assert state.status_code==200 and state.json()['inventory']['backend']=='firestore'
        row=state.json()['items'][0]
        denied=client.request('DELETE',c.ITEMS+'/'+str(item_id),json={})
        assert denied.status_code==409
        removed=client.request('DELETE',c.ITEMS+'/'+str(item_id),json={'confirm_name':row['name'],'expected_location_id':row['location_id']})
        assert removed.status_code==200
        assert client.get(c.STATE).json()['items']==[]
        cloud.fail_read=True
        failed=client.get(c.STATE)
        assert failed.status_code==503 and failed.headers['cache-control']=='no-store'
        assert 'private cloud' not in failed.text
