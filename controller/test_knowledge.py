"""Isolated API and subprocess regressions: no controller lifecycle or robots."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi import HTTPException
from web.routes import knowledge as api
from web.knowledge_jobs import KnowledgeJobs, run_command


class Documents(unittest.IsolatedAsyncioTestCase):
    async def test_crud_collisions_extensions_and_decoding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(api, 'KNOWLEDGE_DIR', Path(directory)), patch.object(api.jobs, 'request') as request:
            saved = await api.save_doc(api.SaveDocRequest(filename='Lab.TXT', content='first'))
            self.assertEqual(saved['filename'], 'Lab.txt')
            with self.assertRaises(HTTPException) as error:
                await api.save_doc(api.SaveDocRequest(filename='lab.txt', content='lost'))
            self.assertEqual(error.exception.status_code, 409)
            await api.update_doc('Lab.txt', api.UpdateDocRequest(content='second'))
            self.assertEqual((await api.get_doc_content('Lab.txt'))['content'], 'second')
            Path(directory, 'Legacy.TXT').write_text('legacy')
            Path(directory, 'broken.txt').write_bytes(b'\xff')
            self.assertEqual((await api.list_docs())['count'], 3)
            with self.assertRaises(HTTPException) as error:
                await api.get_doc_content('broken.txt')
            self.assertEqual(error.exception.status_code, 422)
            with self.assertRaises(HTTPException):
                await api.save_doc(api.SaveDocRequest(filename='../outside.txt', content='bad'))
            await api.delete_doc('Lab.txt')
            self.assertFalse(Path(directory, 'Lab.txt').exists())
            self.assertEqual(request.call_count, 3)

    async def test_process_timeout_kills_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'survived'
            script = Path(directory) / 'child.py'
            script.write_text('import time\nfrom pathlib import Path\ntime.sleep(1)\nPath(' + repr(str(marker)) + ').touch()\n')
            start = asyncio.get_running_loop().time()
            with self.assertRaises(asyncio.TimeoutError):
                await run_command(['bash', '-c', '"$1" "$2" & wait', '_', sys.executable, script], timeout=.1)
            self.assertLess(asyncio.get_running_loop().time() - start, .9)
            await asyncio.sleep(1.1)
            self.assertFalse(marker.exists())

    async def test_jobs_reconcile_changes_during_build_and_failed_retry(self):
        jobs = KnowledgeJobs('.', '.')
        version, indexed, builds = 1, 0, 0
        entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def command(action, payload=None):
            nonlocal indexed, version, builds
            if action == 'status':
                if indexed == 2:
                    settled.set()
                return {'ready': bool(indexed), 'stale': indexed != version, 'compatible': True,
                        'source_fingerprint': str(version)}
            builds += 1
            snapshot = version
            if builds == 1:
                entered.set()
                await release.wait()
            indexed = snapshot
        jobs.command = command
        jobs.start()
        await asyncio.wait_for(entered.wait(), 1)
        version = 2
        jobs.request()
        release.set()
        await asyncio.wait_for(settled.wait(), 1)
        self.assertEqual(builds, 2)
        await jobs.stop()
        # Restart reconstructs readiness without an unnecessary rebuild.
        jobs.start()
        await asyncio.sleep(.01)
        self.assertEqual(builds, 2)
        await jobs.stop()

    async def test_failure_waits_for_retry_and_stop_cancels_build(self):
        jobs = KnowledgeJobs('.', '.')
        failed, entered = asyncio.Event(), asyncio.Event()
        builds = 0
        async def command(action, payload=None):
            nonlocal builds
            if action == 'status':
                return {'ready': True, 'stale': True, 'compatible': True, 'source_fingerprint': 'unchanged'}
            builds += 1
            if builds == 1:
                failed.set()
                raise RuntimeError('Embedding offline')
            entered.set()
            await asyncio.Event().wait()
        jobs.command = command
        jobs.start()
        await asyncio.wait_for(failed.wait(), 1)
        await asyncio.sleep(.02)
        self.assertEqual(jobs.status()['job']['state'], 'failed')
        self.assertEqual(builds, 1)
        jobs.request()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(builds, 2)
        await asyncio.wait_for(jobs.stop(), 1)

    async def test_memory_api_and_error_mapping(self):
        with patch.object(api.jobs, 'command', new_callable=AsyncMock) as command:
            command.return_value = {'memories': [], 'count': 0}
            self.assertEqual((await api.list_memories(offset=0, limit=20))['count'], 0)
            command.assert_awaited_with('memories', {'offset': 0, 'limit': 20})
            command.return_value = {'id': 'one', 'fact': 'Updated'}
            self.assertEqual((await api.update_memory('one', api.UpdateMemoryRequest(fact='Updated')))['fact'], 'Updated')
            for error, status in [('Memory not found', 404), ('Another memory already contains this fact', 409), ('Embedding offline', 503)]:
                command.side_effect = RuntimeError(error)
                with self.assertRaises(HTTPException) as caught:
                    await api.delete_memory('missing')
                self.assertEqual(caught.exception.status_code, status)


if __name__ == '__main__':
    unittest.main()
