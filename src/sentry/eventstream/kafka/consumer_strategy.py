import logging
from typing import Any, Mapping, Optional

from arroyo.backends.kafka.consumer import KafkaPayload
from arroyo.processing.strategies import (
    CommitOffsets,
    ProcessingStrategy,
    ProcessingStrategyFactory,
    RunTaskInThreads,
)
from arroyo.types import Commit, Message, Partition

from sentry import options
from sentry.eventstream.base import GroupStates
from sentry.eventstream.kafka.postprocessworker import _sampled_eventstream_timer
from sentry.eventstream.kafka.protocol import (
    get_task_kwargs_for_message,
    get_task_kwargs_for_message_from_headers,
)
from sentry.tasks.post_process import post_process_group
from sentry.utils import metrics
from sentry.utils.cache import cache_key_for_event

_DURATION_METRIC = "eventstream.duration"

logger = logging.getLogger(__name__)


def dispatch_post_process_group_task(
    event_id: str,
    project_id: int,
    group_id: Optional[int],
    is_new: bool,
    is_regression: Optional[bool],
    is_new_group_environment: bool,
    primary_hash: Optional[str],
    queue: str,
    skip_consume: bool = False,
    group_states: Optional[GroupStates] = None,
    occurrence_id: Optional[str] = None,
) -> None:
    if skip_consume:
        logger.info("post_process.skip.raw_event", extra={"event_id": event_id})
    else:
        cache_key = cache_key_for_event({"project": project_id, "event_id": event_id})

        post_process_group.apply_async(
            kwargs={
                "is_new": is_new,
                "is_regression": is_regression,
                "is_new_group_environment": is_new_group_environment,
                "primary_hash": primary_hash,
                "cache_key": cache_key,
                "group_id": group_id,
                "group_states": group_states,
                "occurrence_id": occurrence_id,
            },
            queue=queue,
        )


def _get_task_kwargs(message: Message[KafkaPayload]) -> Optional[Mapping[str, Any]]:
    use_kafka_headers = options.get("post-process-forwarder:kafka-headers")

    if use_kafka_headers:
        try:
            with _sampled_eventstream_timer(instance="get_task_kwargs_for_message_from_headers"):
                return get_task_kwargs_for_message_from_headers(message.payload.headers)
        except Exception as error:
            logger.warning("Could not forward message: %s", error, exc_info=True)
            with metrics.timer(_DURATION_METRIC, instance="get_task_kwargs_for_message"):
                return get_task_kwargs_for_message(message.payload.value)
    else:
        with metrics.timer(_DURATION_METRIC, instance="get_task_kwargs_for_message"):
            return get_task_kwargs_for_message(message.payload.value)


def _get_task_kwargs_and_dispatch(message: Message[KafkaPayload]) -> None:
    task_kwargs = _get_task_kwargs(message)
    if not task_kwargs:
        return None

    dispatch_post_process_group_task(**task_kwargs)


class PostProcessForwarderStrategyFactory(ProcessingStrategyFactory[KafkaPayload]):
    def __init__(
        self,
        concurrency: int,
        max_pending_futures: int,
    ):
        self.__concurrency = concurrency
        self.__max_pending_futures = max_pending_futures

    def create_with_partitions(
        self,
        commit: Commit,
        partitions: Mapping[Partition, int],
    ) -> ProcessingStrategy[KafkaPayload]:
        return RunTaskInThreads(
            _get_task_kwargs_and_dispatch,
            self.__concurrency,
            self.__max_pending_futures,
            CommitOffsets(commit),
        )
