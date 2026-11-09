from __future__ import annotations

import asyncio
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from web import state
from web.knowledge_jobs import KnowledgeJobs

router = APIRouter()
KNOWLEDGE_DIR = state.CONFIG_DIR / 'knowledge'
PROJECT_ROOT = state.BASE_DIR.parent
jobs = KnowledgeJobs(PROJECT_ROOT, KNOWLEDGE_DIR)


class SaveDocRequest(BaseModel):
    filename: str
    content: str


class UpdateDocRequest(BaseModel):
    content: str


def _safe_filename(name):
    if '/' in name or '\\' in name or '\x00' in name:
        raise HTTPException(400, 'Filename must not contain a path')
    stem = re.sub(r'[^A-Za-z0-9_.\- ]+', '_', name.strip()).strip('._- ')
    stem = stem.replace(' ', '_')
    if not stem:
        raise HTTPException(400, 'Invalid filename')
    return stem[:-4] + '.txt' if stem.lower().endswith('.txt') else stem + '.txt'


def _path(filename):
    if Path(filename).name != filename or '\\' in filename or '\x00' in filename:
        raise HTTPException(400, 'Invalid filename')
    path = KNOWLEDGE_DIR / filename
    if path.suffix.lower() != '.txt' or not path.is_file() or path.is_symlink():
        raise HTTPException(404, f'Document not found: {filename}')
    return path


def _read(path):
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise HTTPException(422, f'{path.name} is not valid UTF-8 text') from exc


@contextmanager
def _documents_lock():
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    with (KNOWLEDGE_DIR / '.documents.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _write(path, content):
    fd, temporary = tempfile.mkstemp(dir=KNOWLEDGE_DIR, prefix='.document-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _list_docs():
    docs = []
    if not KNOWLEDGE_DIR.exists():
        return docs
    for path in sorted(KNOWLEDGE_DIR.iterdir()):
        if path.suffix.lower() != '.txt' or not path.is_file() or path.is_symlink():
            continue
        try:
            content = _read(path)
            error = None
        except HTTPException as exc:
            content, error = '', exc.detail
        stat = path.stat()
        docs.append({'filename': path.name, 'size_bytes': stat.st_size,
                     'modified_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                     'word_count': len(content.split()), 'preview': content[:300], 'error': error})
    return docs


@router.get('/knowledge')
async def serve_knowledge_page():
    return FileResponse(state.STATIC_DIR / 'knowledge.html')


@router.get('/api/knowledge/docs')
async def list_docs():
    docs = _list_docs()
    return {'docs': docs, 'count': len(docs), 'docs_dir': str(KNOWLEDGE_DIR)}


@router.get('/api/knowledge/docs/{filename}/content')
async def get_doc_content(filename: str):
    path = _path(filename)
    return {'filename': path.name, 'content': _read(path)}


@router.post('/api/knowledge/docs', status_code=201)
async def save_doc(request: SaveDocRequest):
    filename = _safe_filename(request.filename)
    with _documents_lock():
        # Also avoid collisions with existing uppercase extensions.
        if any(p.name.casefold() == filename.casefold() for p in KNOWLEDGE_DIR.iterdir()):
            raise HTTPException(409, 'Document already exists. Open Edit to change its contents.')
        _write(KNOWLEDGE_DIR / filename, request.content)
    jobs.request()
    return {'status': 'success', 'filename': filename}


@router.put('/api/knowledge/docs/{filename}')
async def update_doc(filename: str, request: UpdateDocRequest):
    with _documents_lock():
        _write(_path(filename), request.content)
    jobs.request()
    return {'status': 'success', 'filename': filename}


@router.delete('/api/knowledge/docs/{filename}')
async def delete_doc(filename: str):
    with _documents_lock():
        _path(filename).unlink()
    jobs.request()
    return {'status': 'success', 'removed': filename}


@router.post('/api/knowledge/reindex', status_code=202)
async def reindex_knowledge():
    return jobs.request()


@router.get('/api/knowledge/status')
async def index_status():
    jobs.start()
    return jobs.status()


class UpdateMemoryRequest(BaseModel):
    fact: str


async def _memory_command(action, payload):
    try:
        return await jobs.command(action, payload)
    except RuntimeError as exc:
        message = str(exc)
        code = 404 if 'Memory not found' in message else 409 if 'already contains' in message else 400 if 'must not be empty' in message else 503
        raise HTTPException(code, message) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, 'Memory operation timed out') from exc


@router.get('/api/knowledge/memories')
async def list_memories(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return await _memory_command('memories', {'offset': offset, 'limit': limit})


@router.put('/api/knowledge/memories/{memory_id}')
async def update_memory(memory_id: str, request: UpdateMemoryRequest):
    return await _memory_command('update-memory', {'id': memory_id, 'fact': request.fact})


@router.delete('/api/knowledge/memories/{memory_id}')
async def delete_memory(memory_id: str):
    return await _memory_command('delete-memory', {'id': memory_id})
