import json
import logging
from threading import Event, Thread

logger = logging.getLogger(__name__)

class ReplayWorker:
    def __init__(self, replay, poll_seconds=1):
        self.replay, self.poll_seconds = replay, poll_seconds
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
