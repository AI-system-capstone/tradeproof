from common import contracts as C
import json
import logging
from threading import Event, Thread
from sqlalchemy import select
from b_registry.db import Import

logger = logging.getLogger(__name__)

class VerificationWorker:
    def __init__(self, service, replay=None, poll_seconds=1):
        self.service, self.replay, self.poll_seconds = service, replay, poll_seconds
        self._stop, self._wake = Event(), Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="registry-worker", daemon=True)
        self._thread.start()

    def notify(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join()

    def run_once(self):
        with self.service.db.sessions() as s:
            ids = list(s.scalars(select(Import.import_id).where(Import.status == C.IMPORT_RECEIVED).order_by(Import.created_at)))
        for iid in ids:
            if self._stop.is_set():
                break
            try:
                self.service.process(iid)
            except Exception as error:
                logger.error(json.dumps({"event": "registry_import_retry", "import_id": iid, "error": type(error).__name__}))
        if self.replay:
            self.replay.run_once()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                logger.error(json.dumps({"event": "registry_worker_retry", "error": type(error).__name__}))
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
