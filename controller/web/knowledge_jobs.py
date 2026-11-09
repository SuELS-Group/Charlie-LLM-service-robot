"""Supervise workstation-only RAG commands without importing its ML dependencies."""
from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path


async def run_command(command, *, timeout=300, payload=None):
    process = await asyncio.create_subprocess_exec(
        *map(str, command), start_new_session=True,
        stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    communication = asyncio.create_task(process.communicate(
        json.dumps(payload).encode() if payload is not None else None))
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Descendants can keep pipes open after their parent exits. Always target
        # the session group, even if the immediate child has already exited.
        for sig, grace in ((signal.SIGTERM, 1), (signal.SIGKILL, 1)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(communication), grace)
            except asyncio.TimeoutError:
                continue
            # Kill lingering descendants even if they closed their output pipes.
            if sig == signal.SIGTERM:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            break
        if not communication.done():
            communication.cancel()
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode(errors='replace').strip() or f'Command exited {process.returncode}')
    return json.loads(stdout)


class KnowledgeJobs:
    def __init__(self, root, docs_dir, *, db_path=None):
        self.root, self.docs_dir = Path(root), Path(docs_dir)
        self.db_path = db_path
        self.task = None
        self.pending = False
        self.wakeup = asyncio.Event()
        self.index = {'ready': False, 'stale': True, 'compatible': True,
                      'source_count': 0, 'chunk_count': 0, 'last_success': None}
        self.job = {'state': 'idle', 'error': None}
        self.failed_signature = None

    async def command(self, action, payload=None):
        command = [self.root / 'workstation/.venv/bin/python',
                   self.root / 'scripts/ingest_knowledge.py', action,
                   '--docs-dir', self.docs_dir]
        if self.db_path is not None:
            command += ['--db-path', self.db_path]
        return await run_command(command, timeout=300 if action == 'reindex' else 45, payload=payload)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def request(self):
        self.pending = True
        self.wakeup.set()
        self.start()
        return self.status()

    def status(self):
        return {**self.index, 'pending_changes': self.pending or self.index.get('stale', True),
                'job': dict(self.job)}

    async def _run(self):
        while True:
            try:
                self.index = await self.command('status')
                signature = self.index.get('source_fingerprint')
                needs_build = (not self.index['ready'] or self.index['stale'] or not self.index['compatible'])
                if not needs_build and self.job['state'] == 'failed':
                    self.job = {'state': 'succeeded', 'error': None}
                    self.failed_signature = None
                if self.pending or (needs_build and signature != self.failed_signature):
                    self.pending = False
                    self.job = {'state': 'running', 'error': None}
                    try:
                        await self.command('reindex')
                    except Exception as exc:
                        self.failed_signature = signature
                        self.job = {'state': 'failed', 'error': str(exc) or 'Reindex timed out'}
                    else:
                        self.failed_signature = None
                        self.job = {'state': 'succeeded', 'error': None}
                    # Re-read fingerprints, including edits made during this build.
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.job = {'state': 'failed', 'error': str(exc) or 'Knowledge status timed out'}
            self.wakeup.clear()
            if self.pending:
                continue
            try:
                await asyncio.wait_for(self.wakeup.wait(), 10)
            except asyncio.TimeoutError:
                pass
