from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import Mock

from apscheduler.schedulers.background import BackgroundScheduler

from app.services.scheduler import APSchedulerTransferJobs


def test_adapter_dispatches_occurrence_with_the_same_job_interface(settings):
    backend = BackgroundScheduler(timezone="UTC")
    factory = Mock()
    finished = Event()
    received = []

    def execute(transfer_id, jobs, session_factory, configured_settings):
        received.append((transfer_id, jobs, session_factory, configured_settings))
        finished.set()

    jobs = APSchedulerTransferJobs(backend, execute, factory, settings)
    backend.start()
    try:
        jobs.schedule(
            "stable-occurrence", datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        assert finished.wait(5)
        assert received == [("stable-occurrence", jobs, factory, settings)]
        jobs.remove("stable-occurrence")  # Completed date jobs may already be absent.
        jobs.schedule(
            "future-occurrence", datetime.now(timezone.utc) + timedelta(days=1)
        )
        assert backend.get_job("future-occurrence") is not None
        jobs.remove("future-occurrence")
        assert backend.get_job("future-occurrence") is None
    finally:
        backend.shutdown(wait=True)
