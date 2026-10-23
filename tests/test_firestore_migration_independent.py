"""Independent migration refusal/resume checks; no SDK, cloud or private snapshot reads."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from test_firestore_independent import Cloud, Collection, Reference
from server.firestore_inventory import FirestoreInventory


class MigrationReference(Reference):
    def collection(self,name):
        return MigrationCollection(self.client,self.path+'/'+name)
    def create(self,data):
        if self.path in self.client.data:
            raise RuntimeError('Already exists')
        self.client.data[self.path]=copy.deepcopy(data)
    def update(self,changes):
        for key,value in changes.items():
            part=self.client.data[self.path]
            pieces=key.split('.')
            for prefix in pieces[:-1]:
                part=part[prefix]
            part[pieces[-1]]=value


class MigrationCollection(Collection):
    def document(self,name):
        return MigrationReference(self.client,self.path+'/'+name)
    def limit(self,size):
        self.max_size=size
        return self
    def stream(self,transaction=None):
        rows=super().stream(transaction)
        return rows[:getattr(self,'max_size',len(rows))]


class MigrationCloud(Cloud):
    def document(self,path):
        return MigrationReference(self,path)
    def collection(self,name):
        return MigrationCollection(self,name)


@pytest.fixture
def migration(monkeypatch):
    path=Path(__file__).resolve().parents[1]/'scripts'/'migrate_firestore.py'
    spec=importlib.util.spec_from_file_location('independent_migration',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module,'FirestoreInventory',lambda client,household_id:FirestoreInventory(
        client=client,household_id=household_id,transactional=client.transactional))
    module._qa_real_validate_layout=module.validate_layout
    # Only check validation ordering here; geometry belongs to the companion Studio suite.
    # This independent stub keeps original-repo CI independent of a sibling checkout.
    def validate_stub(layout):
        if 'canvas' not in layout:
            raise ValueError('Synthetic invalid layout marker')
    monkeypatch.setattr(module,'validate_layout',validate_stub)
    return module


@pytest.fixture
def snapshot():
    return {'state':{'items':[],'shopping':[]},'locations':[{'id':17,'name':'Synthetic shelf'}],
            'layout':{'revision':9,'name':'Synthetic map','canvas':{'width':1200,'height':900,'mm_per_unit':10},
                      'rooms':[],'walls':[],'openings':[],'objects':[]}}


@pytest.mark.parametrize('collection',['items','locations','shopping','maps','meta'])
def test_independent_nonempty_destination_without_marker_is_completely_untouched(migration,snapshot,collection):
    client=MigrationCloud()
    client.data[f'households/independent_qa/{collection}/existing']={'sentinel':'existing'}
    before=copy.deepcopy(client.data)
    with pytest.raises(RuntimeError,match='nonempty'):
        migration.migrate(snapshot,client,'independent_qa')
    assert client.data==before


def test_independent_invalid_layout_cannot_even_create_prepared_marker(migration,snapshot):
    client=MigrationCloud()
    snapshot['layout']={'revision':9}
    with pytest.raises(Exception):
        migration.migrate(snapshot,client,'independent_qa')
    assert client.data=={}


def test_independent_matching_prepared_marker_can_resume_and_repeat(migration,snapshot):
    client=MigrationCloud()
    digest=hashlib.sha256(json.dumps(snapshot,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    client.data['households/independent_qa']={'migration':{'sha256':digest,'phase':'prepared'}}
    result=migration.migrate(snapshot,client,'independent_qa')
    assert result=={'items':0,'shopping':0,'locations':1,'revision':9}
    before=copy.deepcopy(client.data)
    assert migration.migrate(snapshot,client,'independent_qa')==result
    assert client.data==before


def test_independent_different_snapshot_cannot_replace_existing_marker_or_documents(migration,snapshot):
    client=MigrationCloud()
    migration.migrate(snapshot,client,'independent_qa')
    before=copy.deepcopy(client.data)
    snapshot['layout']['revision']=10
    with pytest.raises(RuntimeError,match='different data'):
        migration.migrate(snapshot,client,'independent_qa')
    assert client.data==before


@pytest.mark.parametrize('household',['home/maps/accidental','../home','x'*101])
def test_independent_invalid_household_cannot_create_any_destination_marker(migration,snapshot,household):
    client=MigrationCloud()
    with pytest.raises((ValueError,RuntimeError)):
        migration.migrate(snapshot,client,household)
    assert client.data=={}


def test_independent_missing_companion_schema_fails_before_any_write(migration,snapshot,tmp_path,monkeypatch):
    client=MigrationCloud()
    monkeypatch.setattr(migration,'ROOT',tmp_path/'original-without-sibling')
    monkeypatch.setattr(migration,'validate_layout',migration._qa_real_validate_layout)
    with pytest.raises(RuntimeError,match='companion joyful-home-map'):
        migration.migrate(snapshot,client,'independent_qa')
    assert client.data=={}
