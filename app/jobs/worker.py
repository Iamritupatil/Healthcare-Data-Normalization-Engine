"""The polling worker: `python -m app.jobs.worker`.

Claims one job at a time and runs it to the end. On SIGTERM (docker compose stop) it
finishes the current chunk, puts the job back as pending, and exits.
"""

import signal
import threading
from types import FrameType

from app.config import get_settings
from app.jobs.runner import (
    JobContext,
    claim_next_job,
    get_job_runner,
    is_transient,
    mark_failed,
    process_job,
)
from app.observability.logging import configure_logging, exception_summary, get_logger
from app.pipeline.mapping import load_registry
from app.services.verifiers import get_verifier

log = get_logger(__name__)
DB_DOWN_BACKOFF_SECONDS = 5.0


def run(stop: threading.Event) -> None:
    settings = get_settings()
    context = JobContext(
        registry=load_registry(settings.mappings_dir),
        settings=settings,
        verifier=get_verifier(settings),
        runner=get_job_runner(settings),
        should_stop=stop.is_set,
    )
    log.info("worker_started", sources=context.registry.sources(), verifier=context.verifier.name)
    while not stop.is_set():
        try:
            claimed = claim_next_job(settings)
        except Exception as error:
            if not is_transient(error):
                raise
            log.warning("claim_failed", **exception_summary(error))
            stop.wait(DB_DOWN_BACKOFF_SECONDS)
            continue
        if claimed is None:
            stop.wait(settings.worker_poll_interval_seconds)
            continue
        job_id, org_id = claimed
        try:
            process_job(job_id, org_id, context)
        except Exception as error:
            log.error("job_failed", job_id=str(job_id), **exception_summary(error))
            try:
                mark_failed(job_id, org_id, error)
            except Exception as mark_error:
                # The job stays 'running'; once its heartbeat is stale another worker retries it.
                log.error("mark_failed_failed", job_id=str(job_id), **exception_summary(mark_error))
    log.info("worker_stopped")


def main() -> None:
    configure_logging(get_settings().log_level)
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("stop_requested", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(stop)


if __name__ == "__main__":
    main()
