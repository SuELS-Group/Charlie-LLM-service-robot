"""One bounded read at a time; timed-out workers cannot accumulate in a queue."""
import threading


class KnowledgeQueryBusy(RuntimeError):
    pass


class BoundedKnowledgeQuery:
    def __init__(self):
        self.lock = threading.Lock()

    def run(self, operation, timeout=10.):
        if not self.lock.acquire(blocking=False):
            raise KnowledgeQueryBusy('Previous knowledge query is still finishing')
        finished, result = threading.Event(), {}
        def work():
            try:
                result['value'] = operation()
            except Exception as exc:
                result['error'] = exc
            finally:
                self.lock.release()
                finished.set()
        worker = threading.Thread(target=work, name='knowledge-query', daemon=True)
        try:
            worker.start()
        except Exception:
            self.lock.release()
            raise
        if not finished.wait(timeout):
            raise TimeoutError('Knowledge query exceeded its total budget')
        if 'error' in result:
            raise result['error']
        return result['value']
