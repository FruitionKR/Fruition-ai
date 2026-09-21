from app.modules.model_usage.application.read_usage import ReadUsageUseCase
from app.modules.model_usage.infrastructure.usage_ledger import PostgresUsageRepository


def get_read_usage():
    return ReadUsageUseCase(PostgresUsageRepository())
