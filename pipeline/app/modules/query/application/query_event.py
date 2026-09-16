import logging

from app.modules.query.application.ports import QueryEventPublisherPort

logger = logging.getLogger(__name__)


def publish_query_event(
    event_publisher: QueryEventPublisherPort | None,
    stage: str,
    message: str,
    data: dict[str, object] | None = None,
) -> None:
    if event_publisher is None:
        return
    try:
        event_publisher.publish(stage, message, data)
    except Exception as exc:
        # 진행 이벤트는 유실을 허용하지만, 조용히 사라지면 발행이 계속 실패해도 알 수 없다.
        logger.warning("[질의 진행 이벤트 발행 실패] stage=%s errorType=%s", stage, type(exc).__name__)
