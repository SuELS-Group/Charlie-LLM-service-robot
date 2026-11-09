import sys
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from workstation_common.knowledge_store import KnowledgeStore


class Embeddings:
    def embed(self, **kwargs):
        assert kwargs["truncate"] is False
        return SimpleNamespace(embeddings=[[1., 0., 0.]])


@pytest.fixture
def store(tmp_path):
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'lab.TXT').write_text('The lab is on the third floor.')
    config = SimpleNamespace(knowledge_db_path='db', knowledge_embed_model='test',
        knowledge_collection='knowledge', memory_collection='memory',
        knowledge_chunk_size=10, knowledge_chunk_overlap=2,
        knowledge_distance_threshold=.2, memory_distance_threshold=.4,
        ollama_base_url='http://localhost:11434')
    return KnowledgeStore(config, tmp_path, docs_dir=docs, ollama=Embeddings())


def test_failure_preserves_index_and_empty_sources_clear_it(store):
    original = store.rebuild()
    store.embed = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
    with pytest.raises(RuntimeError, match='offline'):
        store.rebuild()
    assert store.read_manifest() == original
    assert store.status()['chunk_count'] == 1
    (store.docs_dir / 'lab.TXT').unlink()
    store.rebuild()
    assert store.status()['chunk_count'] == 0
    assert store.query('anything') == []


def test_bad_encoding_and_invalid_chunk_config_preserve_index(store):
    original = store.rebuild()
    (store.docs_dir / 'bad.txt').write_bytes(b'\xff')
    with pytest.raises(UnicodeDecodeError):
        store.rebuild()
    assert store.read_manifest() == original
    (store.docs_dir / 'bad.txt').unlink()
    store.config.knowledge_chunk_overlap = store.config.knowledge_chunk_size
    with pytest.raises(ValueError):
        store.rebuild()
    assert store.read_manifest() == original


def test_reader_uses_previous_generation_and_other_builder_is_rejected(store):
    store.rebuild()
    original = store.read_manifest()
    entered, release = Event(), Event()
    def embedding(*a, **k):
        if k.get('passage'):
            entered.set()
            assert release.wait(5)
        return [1., 0., 0.]
    store.embed = embedding
    with ThreadPoolExecutor() as pool:
        future = pool.submit(store.rebuild)
        assert entered.wait(5)
        try:
            assert store.query('lab')[0]['source'] == 'lab.TXT'
            assert store.read_manifest() == original
            with pytest.raises(BlockingIOError):
                store.rebuild()
        finally:
            release.set()
        future.result()
    assert store.read_manifest() != original


def test_missing_legacy_and_source_fingerprints(store):
    assert not store.status()['ready']
    with pytest.raises(RuntimeError, match='not initialized'):
        store.query('lab')
    legacy = store.client.create_collection('knowledge', metadata={'hnsw:space': 'cosine'})
    legacy.add(ids=['old'], documents=['Old fact'], embeddings=[[1., 0., 0.]])
    assert store.query('old')[0]['id'] == 'old'
    store.rebuild()
    assert not store.status()['stale']
    (store.docs_dir / 'lab.TXT').write_text('Changed')
    assert store.status()['stale']
    assert legacy.get()['documents'] == ['Old fact']


def test_memory_duplicate_edit_failure_and_delete(store):
    first = store.store_memory('Meeting at 10 on Friday')
    duplicate = store.store_memory('  MEETING   at 10 on Friday ')
    assert duplicate['id'] == first['id']
    assert duplicate['duplicate']
    updated = store.update_memory(first['id'], 'Meeting at 11 on Friday')
    assert updated['stored_at'] == first['stored_at']
    assert updated['modified_at']
    assert store.recall('meeting')[0]['fact'] == updated['fact']
    store.embed = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
    with pytest.raises(RuntimeError):
        store.update_memory(first['id'], 'Lost change')
    assert store.list_memories()['memories'][0]['fact'] == updated['fact']
    store.delete_memory(first['id'])
    assert store.recall('meeting') == []
    with pytest.raises(KeyError):
        store.delete_memory(first['id'])


def test_concurrent_duplicate_storage_and_pagination(store):
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(store.store_memory, ['same fact', 'Same Fact', '  SAME  fact ']))
    assert len({r['id'] for r in records}) == 1
    other = store.store_memory('Other fact')
    assert store.list_memories(limit=1)['count'] == 2
    assert len(store.list_memories(offset=1, limit=1)['memories']) == 1
    with pytest.raises(ValueError, match='already contains'):
        store.update_memory(other['id'], 'same fact')
    with pytest.raises(ValueError):
        store.store_memory('   ')



def test_prefixes_and_legacy_representations(store):
    inputs = []
    def embed(**kwargs):
        inputs.append(kwargs['input'])
        assert kwargs['truncate'] is False
        return SimpleNamespace(embeddings=[[1., 0., 0.]])
    store.ollama.embed = embed
    store.rebuild()
    store.query('lab')
    assert inputs[0].startswith('passage: ')
    assert inputs[-1] == 'query: lab'
    store.embed('legacy', format=store.legacy_format())
    assert inputs[-1] == 'legacy'


def test_context_overflow_splits_without_losing_words_or_hiding_errors(store):
    from ollama import ResponseError
    def embed(text, **kwargs):
        if len(text.split()) > 4 or len(text) > 80:
            raise ResponseError('the input length exceeds the context length', 400)
        return [1., 0., 0.]
    store.embed = embed
    original = ' '.join('word' + str(i) for i in range(40))
    chunks = list(store.embedded_passages(original))
    assert set(original.split()) == set(' '.join(text for text, _ in chunks).split())
    assert len(list(store.embedded_passages('漢' * 600))) > 1
    store.embed = lambda *a, **k: (_ for _ in ()).throw(ResponseError('model missing', 404))
    with pytest.raises(ResponseError, match='model missing'):
        list(store.embedded_passages('hello'))


def test_memory_migration_preserves_ids_metadata_and_failed_copy(store):
    legacy = store.client.create_collection('memory', metadata={'hnsw:space': 'cosine'})
    legacy.add(ids=['old-id'], documents=['Old saved fact'], embeddings=[[1., 0., 0.]],
               metadatas=[{'stored_at': '2020-01-01', 'custom': 'preserved'}])
    original_embed = store.embed
    store.embed = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline'))
    with pytest.raises(RuntimeError):
        store.migrate_memories()
    assert store.read_manifest('memory') is None
    assert legacy.count() == 1
    store.embed = original_embed
    manifest = store.migrate_memories()
    assert manifest['format']['schema'] == 'e5-v2'
    assert store.list_memories()['memories'] == [{'id': 'old-id', 'fact': 'Old saved fact', 'stored_at': '2020-01-01', 'custom': 'preserved'}]
    assert store.recall('fact')[0]['id'] == 'old-id'
    assert legacy.count() == 1
