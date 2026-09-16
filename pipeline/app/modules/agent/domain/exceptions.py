class AgentTurnRouteContractError(ValueError):
    def __init__(self, failures: list[str]) -> None:
        super().__init__("Agent turn route contract failed: " + "; ".join(failures))
        self.failures = failures


class AgentConfigurationError(RuntimeError):
    """의존성 주입이나 기능 플래그가 갖춰지지 않아 처리할 수 없는 상태.

    요청이 잘못된 것이 아니므로 400이 아니라 500으로 응답해야 한다.
    400으로 내보내면 호출부가 재시도하지 않고 사용자에게 내부 메시지가 노출된다.
    """
