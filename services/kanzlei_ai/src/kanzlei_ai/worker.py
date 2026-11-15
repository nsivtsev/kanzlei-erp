"""Independent Celery entry point; document tasks are added in the ingestion stage."""

from celery import Celery

from kanzlei_ai.settings import Settings

app = Celery("kanzlei_ai", broker=Settings().redis_url.get_secret_value())
app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
)
