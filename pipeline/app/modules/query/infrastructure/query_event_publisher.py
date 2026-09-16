from app.modules.query.application.ports import QueryEventPublisherPort


class NoOpQueryEventPublisher(QueryEventPublisherPort):
    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        return None
