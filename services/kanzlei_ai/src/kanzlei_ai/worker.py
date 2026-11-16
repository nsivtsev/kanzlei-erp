"""Celery worker for validating and staging source metadata."""

import logging

from celery import Celery

from kanzlei_ai.db import engine_from_settings
from kanzlei_ai.jobs import claim, complete, fail
from kanzlei_ai.settings import Settings

app = Celery("kanzlei_ai", broker=Settings().redis_url.get_secret_value())
app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=60,
    task_time_limit=90,
    broker_transport_options={
        "visibility_timeout": 300,
        "socket_connect_timeout": 3,
        "socket_timeout": 5,
        "retry_on_timeout": False,
    },
    broker_connection_timeout=3,
    task_publish_retry=False,
)


@app.task(name="kanzlei_ai.stage_metadata")
def stage_metadata(job_id):
    engine = None
    token = None
    try:
        engine = engine_from_settings(Settings())
        token = claim(engine, job_id)
        if token:
            complete(engine, job_id, token)
    except Exception:
        if token and engine is not None:
            try:
                fail(engine, job_id, token)
            except Exception:
                pass  # Expired-lease recovery handles an unavailable database.
        logging.getLogger(__name__).error("Metadata staging failed; recovery will retry")
    finally:
        if engine is not None:
            engine.dispose()
