import unittest

from app.modules.query.application.query_event import publish_query_event


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object] | None]] = []

    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        self.events.append((stage, message, data))


class FailingEventPublisher:
    def publish(self, stage: str, message: str, data: dict[str, object] | None = None) -> None:
        raise RuntimeError("callback failed")


class QueryEventTest(unittest.TestCase):
    def test_publishes_query_event_when_publisher_exists(self) -> None:
        publisher = RecordingEventPublisher()

        publish_query_event(publisher, "query_started", "질의 처리를 시작했습니다.", {"question": "RAG가 뭐야?"})

        self.assertEqual(
            publisher.events,
            [("query_started", "질의 처리를 시작했습니다.", {"question": "RAG가 뭐야?"})],
        )

    def test_ignores_missing_or_failing_publisher(self) -> None:
        publish_query_event(None, "query_started", "질의 처리를 시작했습니다.")
        publish_query_event(FailingEventPublisher(), "query_started", "질의 처리를 시작했습니다.")

    def test_failing_publisher_is_logged(self) -> None:
        """유실은 허용하되 조용히 사라지면 발행이 계속 실패해도 알 수 없다."""
        with self.assertLogs("app.modules.query.application.query_event", level="WARNING") as logs:
            publish_query_event(FailingEventPublisher(), "query_started", "질의 처리를 시작했습니다.")

        self.assertEqual(len(logs.output), 1)
        self.assertIn("stage=query_started", logs.output[0])
        self.assertIn("errorType=RuntimeError", logs.output[0])


if __name__ == "__main__":
    unittest.main()
