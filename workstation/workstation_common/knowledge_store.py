"""Local RAG storage shared by the CLI and MCP tools.

Immutable document generations are published under a short reader/writer lock.
The build lock spans embedding; queries can use the predecessor throughout.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import logging
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from chromadb.config import Settings
from ollama import Client, ResponseError


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class KnowledgeStore:
    def __init__(self, config, root, *, docs_dir=None, db_path=None, client=None, ollama=None):
        self.config = config
        self.root = Path(root)
        self.docs_dir = Path(docs_dir or self.root / 'controller/config/knowledge')
        self.db_path = Path(db_path or self.root / config.knowledge_db_path)
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.model = config.knowledge_embed_model
        self.collection_name = config.knowledge_collection
        self.client = client or chromadb.PersistentClient(
            path=str(self.db_path), settings=Settings(anonymized_telemetry=False))
        self.ollama = ollama or Client(
            host=config.ollama_base_url.removesuffix('/v1'),
            timeout=float(getattr(config, 'knowledge_embedding_timeout_s', 30)))

    @contextmanager
    def lock(self, name, *, shared=False, blocking=True):
        with (self.db_path / (name + '.lock')).open('a') as stream:
            flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(stream, flags)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def read_manifest(self, kind='knowledge'):
        path = self.db_path / (kind + '_active.json')
        return json.loads(path.read_text()) if path.exists() else None

    def format(self):
        return {'schema': 'e5-v2', 'model': self.model,
                'chunk_size': int(self.config.knowledge_chunk_size),
                'chunk_overlap': int(self.config.knowledge_chunk_overlap)}

    def sources(self):
        # Fingerprint the same bytes that will be embedded, even during edits.
        return {p.name: p.read_bytes() for p in sorted(self.docs_dir.iterdir())
                if p.is_file() and not p.is_symlink() and p.suffix.lower() == '.txt'} if self.docs_dir.exists() else {}

    @staticmethod
    def fingerprints(sources):
        return {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}

    def _collection(self, manifest=None):
        name = manifest['collection'] if manifest else self.collection_name
        if name not in {c.name for c in self.client.list_collections()}:
            raise RuntimeError('Knowledge base not initialized. Run scripts/ingest_knowledge.sh or Reindex in the Knowledge page.')
        return self.client.get_collection(name)

    def status(self):
        sources = self.sources()
        with self.lock('publication', shared=True):
            manifest = self.read_manifest()
            try:
                count = self._collection(manifest).count()
                ready = True
            except RuntimeError:
                count, ready = 0, False
            stale = not manifest or manifest['sources'] != self.fingerprints(sources)
            compatible = bool(manifest and manifest['format'] == self.format())
            memory_manifest = self.read_manifest('memory')
            memory_compatible = bool(memory_manifest and memory_manifest['format'] == self.embedding_format())
            return {'ready': ready, 'stale': stale, 'compatible': compatible and memory_compatible, 'memory_compatible': memory_compatible,
                    'source_count': len(sources), 'chunk_count': count,
                    'source_fingerprint': hashlib.sha256(json.dumps(self.fingerprints(sources), sort_keys=True).encode()).hexdigest(),
                    'indexed_document_count': len(manifest['sources']) if manifest else None,
                    'last_success': manifest['completed_at'] if manifest else None,
                    'generation': manifest['collection'] if manifest else None}

    def embedding_format(self):
        return {'schema': 'e5-v2', 'model': self.model}

    def legacy_format(self):
        return {'schema': 'raw-v1', 'model': self.model}

    def embed(self, text, *, passage=False, format=None):
        representation = format or self.embedding_format()
        schema = representation['schema']
        if schema not in ('raw-v1', 'e5-v2'):
            raise RuntimeError('Unsupported embedding format. Reindex knowledge before querying.')
        prefix = ('passage: ' if passage else 'query: ') if schema == 'e5-v2' else ''
        response = self.ollama.embed(model=representation['model'], input=prefix + text, truncate=False,
            keep_alive=getattr(self.config, 'knowledge_keep_alive', '1h'))
        vector = list(response.embeddings[0])
        if not vector or not all(math.isfinite(value) for value in vector) or not any(vector):
            raise RuntimeError('Embedding model returned an invalid vector')
        return vector

    def embedded_passages(self, text, *, format=None):
        try:
            yield text, self.embed(text, passage=True, format=format)
            return
        except ResponseError as exc:
            if exc.status_code != 400 or 'context length' not in str(exc).lower():
                raise
        # Ollama checks the actual tokenizer budget. Split only on that specific
        # error; never silently truncate or retry unrelated model/server failures.
        words = text.split()
        if len(words) > 1:
            middle = len(words) // 2
            overlap = min(int(self.config.knowledge_chunk_overlap), len(words) // 8)
            parts = (' '.join(words[:middle + overlap]), ' '.join(words[middle - overlap:]))
        elif len(text) > 1:
            middle = len(text) // 2
            overlap = min(64, len(text) // 8)
            parts = (text[:middle + overlap], text[middle - overlap:])
        else:
            raise ValueError('Embedding model cannot fit even a single character')
        for part in parts:
            yield from self.embedded_passages(part, format=format)

    def fact_embedding(self, fact, *, format):
        vectors = [vector for _, vector in self.embedded_passages(fact, format=format)]
        # Keep one stable ID/full fact, including for oversized legacy memories.
        vector = [sum(values) / len(vectors) for values in zip(*vectors)]
        norm = math.sqrt(sum(value * value for value in vector))
        if not norm:
            raise ValueError('Fact embedding has zero magnitude')
        return [value / norm for value in vector]

    def chunks(self, text):
        size, overlap = int(self.config.knowledge_chunk_size), int(self.config.knowledge_chunk_overlap)
        if size <= 0 or not 0 <= overlap < size:
            raise ValueError('Require chunk size > 0 and 0 <= overlap < chunk size')
        words = text.split()
        for start in range(0, len(words), size - overlap):
            yield ' '.join(words[start:start + size])
            if start + size >= len(words):
                break

    def rebuild(self):
        with self.lock('build', blocking=False):
            sources = self.sources()
            name = self.collection_name + '_' + uuid.uuid4().hex
            collection = self.client.create_collection(name, metadata={'hnsw:space': 'cosine'})
            published = False
            try:
                count = 0
                for filename, data in sources.items():
                    chunks = [part for text in self.chunks(data.decode('utf-8'))
                              for part in self.embedded_passages(text)]
                    for index, (text, embedding) in enumerate(chunks):
                        collection.add(ids=[f'{filename}:{index}'], documents=[text],
                                       embeddings=[embedding],
                                       metadatas=[{'source': filename, 'chunk': index, 'total_chunks': len(chunks)}])
                        count += 1
                if collection.count() != count:
                    raise RuntimeError('Index validation failed: chunk count mismatch')
                manifest = {'collection': name, 'sources': self.fingerprints(sources),
                            'format': self.format(), 'completed_at': now(), 'chunk_count': count}
                with self.lock('publication'):
                    previous = self.read_manifest()
                    atomic_json(self.db_path / 'knowledge_active.json', manifest)
                    published = True
                    keep = {name, (previous or {}).get('collection')}
                    for old in self.client.list_collections():
                        if re.fullmatch(re.escape(self.collection_name) + r'_[0-9a-f]{32}', old.name) and old.name not in keep:
                            try:
                                self.client.delete_collection(old.name)
                            except Exception:
                                logging.getLogger(__name__).warning('Could not clean up old knowledge generation %s', old.name, exc_info=True)
                return manifest
            finally:
                if not published:
                    self.client.delete_collection(name)

    def query(self, query):
        with self.lock('publication', shared=True):
            manifest = self.read_manifest()
            collection = self._collection(manifest)
            if not collection.count():
                return []
            embedding = self.embed(query, format=manifest['format'] if manifest else self.legacy_format())
            results = collection.query(query_embeddings=[embedding], n_results=min(3, collection.count()),
                                       include=['documents', 'metadatas', 'distances'])
            threshold = self.config.knowledge_distance_threshold if manifest and manifest['format']['schema'] == 'e5-v2' else getattr(self.config, 'knowledge_legacy_distance_threshold', .2)
            return self.matches(results, float(threshold))

    def query_evidence(self, query, *, require_approval=False):
        """Retrieve a traceable generation; never silently serve edited sources.

        The legacy query API remains available for storage/management checks.
        Interactive answers use this stricter path. A publication lock keeps
        generation identity, metadata and retrieved chunks consistent.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError('Knowledge query must contain 1–2000 characters')
        with self.lock('publication', shared=True):
            manifest = self.read_manifest()
            fingerprints = self.fingerprints(self.sources())
            current = bool(manifest and manifest.get('format') == self.format() and
                           manifest.get('sources') == fingerprints)
            data = {'generation': (manifest or {}).get('collection'), 'fresh': current, 'retrieved_at': now(),
                'source_fingerprint': hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest(),
                'embedding_model': self.model, 'approved': False, 'count': 0, 'chunks': []}
            if not current:
                return {**data, 'evidence_status': 'outdated' if manifest else 'unavailable'}
            approval_path = Path(getattr(self.config, 'knowledge_approval_file',
                'controller/config/knowledge_approval.json'))
            if not approval_path.is_absolute():
                approval_path = self.root / approval_path
            try:
                approval = json.loads(approval_path.read_text())
                data['approved'] = bool(isinstance(approval, dict) and approval.get('schema') == 1 and
                    all(isinstance(approval.get(key), str) and approval[key].strip()
                        for key in ('owner', 'approved_at', 'version', 'generation')) and
                    approval['generation'] == manifest['collection'] and approval.get('sources') == fingerprints)
                if data['approved']:
                    data['approval_version'] = approval['version']
            except (OSError, ValueError, TypeError):
                pass
            if require_approval and not data['approved']:
                return {**data, 'evidence_status': 'unapproved'}
            collection = self._collection(manifest)
            chunks = []
            if collection.count():
                embedding = self.embed(query.strip(), format=manifest['format'])
                results = collection.query(query_embeddings=[embedding],
                    n_results=min(6, collection.count()), include=['documents', 'metadatas', 'distances'])
                chunks = self.matches(results, float(self.config.knowledge_distance_threshold))
            # Editors do not take the publication lock. An edit during embedding
            # must invalidate this reply too, even if the old index still exists.
            if self.fingerprints(self.sources()) != fingerprints:
                return {**data, 'fresh': False, 'evidence_status': 'outdated'}
            return {**data, 'count': len(chunks), 'chunks': chunks,
                    'evidence_status': 'available' if chunks else 'no_evidence'}

    def approval_candidate(self):
        """Return an unsigned review record; do not write/activate approval."""
        with self.lock('publication', shared=True):
            manifest = self.read_manifest()
            fingerprints = self.fingerprints(self.sources())
            if (not manifest or not fingerprints or manifest.get('sources') != fingerprints or
                    manifest.get('format') != self.format()):
                raise ValueError('A nonempty, current index is required before reviewing an approval candidate')
            return {'schema': 1, 'owner': '', 'approved_at': '', 'version': '',
                'generation': manifest['collection'], 'sources': fingerprints}

    @staticmethod
    def matches(results, threshold, *, memory=False):
        matches = []
        for id_, text, meta, distance in zip(results['ids'][0], results['documents'][0],
                                            results['metadatas'][0], results['distances'][0]):
            try:
                distance = float(distance)
            except (ValueError, TypeError):
                continue
            if not math.isfinite(distance) or distance > threshold:
                continue
            matches.append({'id': id_, 'fact' if memory else 'text': text,
                            'distance': round(distance, 4), **(meta or {})})
        return matches

    @staticmethod
    def normalize_fact(fact):
        import unicodedata
        normalized = ' '.join(unicodedata.normalize('NFKC', fact).split()).casefold()
        if not normalized:
            raise ValueError('Fact must not be empty')
        return normalized

    def _memory_collection(self):
        manifest = self.read_manifest('memory')
        name = manifest['collection'] if manifest else self.config.memory_collection
        if name not in {c.name for c in self.client.list_collections()}:
            if manifest:
                raise RuntimeError('Active memory collection is missing; restore the database backup.')
            collection = self.client.create_collection(name, metadata={'hnsw:space': 'cosine'})
            atomic_json(self.db_path / 'memory_active.json', {
                'collection': name, 'format': self.embedding_format(), 'completed_at': now()})
            return collection
        return self.client.get_collection(name)

    @staticmethod
    def _facts(collection):
        data = collection.get(include=['documents', 'metadatas'])
        return [{'id': id_, 'fact': text, **(meta or {})}
                for id_, text, meta in zip(data['ids'], data['documents'], data['metadatas'])]

    def list_memories(self, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError('Require offset >= 0 and limit between 1 and 100')
        with self.lock('memory'):
            records = sorted(self._facts(self._memory_collection()), key=lambda r: (r.get('stored_at', ''), r['id']))
            return {'memories': records[offset:offset + limit], 'count': len(records), 'offset': offset, 'limit': limit}

    def store_memory(self, fact):
        key = self.normalize_fact(fact)
        with self.lock('memory'):
            collection = self._memory_collection()
            for record in self._facts(collection):
                if self.normalize_fact(record['fact']) == key:
                    return {**record, 'duplicate': True}
            representation = (self.read_manifest('memory') or {}).get('format', self.legacy_format())
            embedding = self.fact_embedding(fact, format=representation)
            record = {'id': 'mem_' + uuid.uuid4().hex, 'fact': fact.strip(), 'stored_at': now()}
            collection.add(ids=[record['id']], documents=[record['fact']], embeddings=[embedding],
                           metadatas=[{'stored_at': record['stored_at']}])
            return {**record, 'duplicate': False}

    def update_memory(self, id_, fact):
        key = self.normalize_fact(fact)
        with self.lock('memory'):
            collection = self._memory_collection()
            records = self._facts(collection)
            existing = next((r for r in records if r['id'] == id_), None)
            if existing is None:
                raise KeyError('Memory not found')
            if any(r['id'] != id_ and self.normalize_fact(r['fact']) == key for r in records):
                raise ValueError('Another memory already contains this fact')
            representation = (self.read_manifest('memory') or {}).get('format', self.legacy_format())
            embedding = self.fact_embedding(fact, format=representation)
            metadata = {k: v for k, v in existing.items() if k not in ('id', 'fact')}
            metadata['modified_at'] = now()
            collection.update(ids=[id_], documents=[fact.strip()], embeddings=[embedding], metadatas=[metadata])
            return {'id': id_, 'fact': fact.strip(), **metadata}

    def delete_memory(self, id_):
        with self.lock('memory'):
            collection = self._memory_collection()
            if not collection.get(ids=[id_])['ids']:
                raise KeyError('Memory not found')
            collection.delete(ids=[id_])
            return {'removed': id_}

    def recall(self, query):
        with self.lock('memory'):
            collection = self._memory_collection()
            count = collection.count()
            if not count:
                return []
            representation = (self.read_manifest('memory') or {}).get('format', self.legacy_format())
            results = collection.query(query_embeddings=[self.embed(query, format=representation)], n_results=min(3, count),
                                       include=['documents', 'metadatas', 'distances'])
            threshold = self.config.memory_distance_threshold if representation['schema'] == 'e5-v2' else getattr(self.config, 'memory_legacy_distance_threshold', .4)
            return self.matches(results, float(threshold), memory=True)


    def migrate_memories(self):
        """Copy before publication; the lock prevents losing concurrent fact edits."""
        with self.lock('memory'):
            previous = self.read_manifest('memory')
            if previous and previous['format'] == self.embedding_format():
                return previous
            records = self._facts(self._memory_collection())
            name = self.config.memory_collection + '_' + uuid.uuid4().hex
            collection = self.client.create_collection(name, metadata={'hnsw:space': 'cosine'})
            published = False
            try:
                for record in records:
                    metadata = {k: v for k, v in record.items() if k not in ('id', 'fact')}
                    collection.add(ids=[record['id']], documents=[record['fact']],
                                   embeddings=[self.fact_embedding(record['fact'], format=self.embedding_format())],
                                   metadatas=[metadata] if metadata else None)
                if collection.count() != len(records):
                    raise RuntimeError('Memory migration count mismatch')
                manifest = {'collection': name, 'format': self.embedding_format(), 'completed_at': now()}
                atomic_json(self.db_path / 'memory_active.json', manifest)
                published = True
                # Keep the original collection for recovery. No old fact data is deleted.
                return manifest
            finally:
                if not published:
                    self.client.delete_collection(name)
