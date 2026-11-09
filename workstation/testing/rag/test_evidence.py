import json
from threading import Event

import pytest
from test_store import store
from mcp_server.knowledge_query import BoundedKnowledgeQuery, KnowledgeQueryBusy


def approve(store):
    manifest = store.read_manifest()
    path = store.root / 'controller/config/knowledge_approval.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schema': 1, 'owner': 'Test reviewer', 'version': 'fixture-v1',
        'approved_at': '2026-10-28', 'generation': manifest['collection'], 'sources': manifest['sources']}))


def test_generation_freshness_and_approval_are_returned_together(store):
    manifest = store.rebuild()
    evidence = store.query_evidence('lab')
    assert evidence['generation'] == manifest['collection']
    assert evidence['fresh'] and not evidence['approved'] and evidence['count'] == 1
    assert store.query_evidence('lab', require_approval=True)['evidence_status'] == 'unapproved'
    approve(store)
    assert store.query_evidence('lab', require_approval=True)['approved']


@pytest.mark.parametrize('change', ['edit', 'delete', 'model', 'failed_rebuild'])
def test_outdated_generation_cannot_silently_answer(store, change):
    store.rebuild(); approve(store)
    if change == 'delete': (store.docs_dir / 'lab.TXT').unlink()
    elif change == 'model': store.model = 'changed-model'
    else: (store.docs_dir / 'lab.TXT').write_text('New floor')
    if change == 'failed_rebuild':
        store.embed = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('offline'))
        with pytest.raises(RuntimeError): store.rebuild()
    evidence = store.query_evidence('lab')
    assert evidence['evidence_status'] == 'outdated' and not evidence['chunks']


def test_new_generation_requires_new_approval_even_with_same_sources(store):
    store.rebuild(); approve(store)
    store.rebuild()
    assert store.query_evidence('lab', require_approval=True)['evidence_status'] == 'unapproved'
    approve(store)
    assert store.query_evidence('lab', require_approval=True)['evidence_status'] == 'available'


def test_edit_during_embedding_invalidates_answer(store):
    store.rebuild()
    def edit(*_a, **_k):
        (store.docs_dir / 'lab.TXT').write_text('Edited during lookup')
        return [1., 0., 0.]
    store.embed = edit
    evidence = store.query_evidence('lab')
    assert evidence['evidence_status'] == 'outdated' and not evidence['chunks']


def test_query_deadline_does_not_create_a_queue_or_publish_late_results():
    runner = BoundedKnowledgeQuery()
    release, finished = Event(), Event()
    def slow():
        release.wait(1)
        finished.set()
        return 'late answer'
    try:
        with pytest.raises(TimeoutError): runner.run(slow, timeout=.01)
        with pytest.raises(KnowledgeQueryBusy): runner.run(lambda: pytest.fail('Must not run'), timeout=.01)
    finally:
        release.set()
        assert finished.wait(1)
        # The fixture event marks the operation's end; the runner releases its
        # admission lock immediately afterwards. Wait for that cleanup too.
        assert runner.lock.acquire(timeout=1)
        runner.lock.release()
    assert runner.run(lambda: 'new answer') == 'new answer'


def test_retrieval_failure_is_not_an_empty_result_and_can_recover():
    runner = BoundedKnowledgeQuery()
    with pytest.raises(RuntimeError):
        runner.run(lambda: (_ for _ in ()).throw(RuntimeError('embedding offline')))
    assert runner.run(lambda: {'evidence_status': 'no_evidence'})['evidence_status'] == 'no_evidence'


def test_approval_candidate_is_not_an_approval_and_rejects_stale_index(store):
    manifest = store.rebuild()
    candidate = store.approval_candidate()
    assert candidate['generation'] == manifest['collection'] and not candidate['owner']
    path = store.root / 'controller/config/knowledge_approval.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(candidate))
    assert store.query_evidence('lab', require_approval=True)['evidence_status'] == 'unapproved'
    (store.docs_dir / 'lab.TXT').write_text('Different floor')
    with pytest.raises(ValueError, match='current index'): store.approval_candidate()
