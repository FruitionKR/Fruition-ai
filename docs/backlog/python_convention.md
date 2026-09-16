# AI Coding Spec: FastAPI AI Service with DDD-style Structure

## 0. Purpose

Build a Python FastAPI service that exposes AI inference APIs and can be deployed with Docker.

This document is written for AI coding agents. Follow this spec over generic FastAPI examples.

## 1. Non-negotiable Architecture Rules

1. Use a DDD-inspired modular monolith structure.
2. Use `app/modules/{bounded_context}/` as the feature boundary.
3. Use `application/` as the DDD application layer folder name.
4. Do not rename `application/` to `use_cases/`, `services/`, `handlers/`, or `workflow/`.
5. Do not create a global `app/services/` folder for business use cases.
6. Do not put AI model SDK code inside `domain/` or `application/`.
7. Do not call AI models directly from FastAPI route functions.
8. Load heavy AI models once during app startup, not per request.
9. Use ports in `application/ports.py`; implement them in `infrastructure/`.
10. Keep HTTP, domain, application, and infrastructure concerns separate.

## 2. Required Folder Structure

```text
app/
├─ __init__.py
├─ main.py
├─ core/
│  ├─ __init__.py
│  ├─ config.py
│  └─ logging.py
└─ modules/
   ├─ __init__.py
   └─ prediction/
      ├─ __init__.py
      ├─ domain/
      │  ├─ __init__.py
      │  ├─ entities.py
      │  ├─ value_objects.py
      │  └─ exceptions.py
      ├─ application/
      │  ├─ __init__.py
      │  ├─ ports.py
      │  └─ predict_text.py
      ├─ infrastructure/
      │  ├─ __init__.py
      │  ├─ model_loader.py
      │  └─ local_text_predictor.py
      └─ interfaces/
         ├─ __init__.py
         └─ http/
            ├─ __init__.py
            ├─ routes.py
            ├─ schemas.py
            └─ dependencies.py

tests/
└─ modules/
   └─ prediction/
      ├─ test_predict_text.py
      └─ test_prediction_routes.py

Dockerfile
docker-compose.yml
.dockerignore
.env.example
pyproject.toml
README.md
```

## 3. Meaning of Each Layer

### `domain/`

Contains pure business concepts.

Allowed:

- Entity classes
- Value objects
- Domain-specific validation
- Domain exceptions
- Pure Python logic

Forbidden:

- FastAPI imports
- Pydantic request/response schemas
- Database clients
- AI SDKs such as `torch`, `transformers`, `openai`, `langchain`
- Environment variables
- File system access

### `application/`

Contains use cases and orchestration logic.

Important: `application/` does not mean the FastAPI app. It means the DDD application layer.

Allowed:

- Use case classes
- Application services
- Port interfaces
- Transaction or workflow orchestration
- Calling domain objects
- Calling external systems only through ports

Forbidden:

- FastAPI `Request`, `Response`, `Depends`, `APIRouter`
- AI SDK imports
- DB client imports
- Direct file loading
- HTTP-specific request/response schemas

### `infrastructure/`

Contains technical implementations.

Allowed:

- AI model loading
- Local model inference implementation
- OpenAI or external LLM API clients
- Vector DB clients
- Database repositories
- File system access
- Implementations of application ports

Forbidden:

- FastAPI route definitions
- Business rules that belong to `domain/`
- Request/response schema definitions

### `interfaces/http/`

Contains FastAPI-specific HTTP code.

Allowed:

- `APIRouter`
- Request/response Pydantic schemas
- Dependency injection wiring
- HTTP exception mapping
- Calling application use cases

Forbidden:

- Direct AI model inference
- Direct database queries
- Domain business rules
- Heavy startup logic

### `core/`

Contains cross-cutting application configuration.

Allowed:

- Settings
- Logging configuration
- Common app constants

Forbidden:

- Business rules
- Use case logic
- Model inference logic

## 4. Dependency Direction

Follow this dependency direction:

```text
interfaces/http  →  application  →  domain
infrastructure   →  application ports
main.py          →  interfaces/http + infrastructure wiring
```

Rules:

1. `domain/` must not import from `application/`, `infrastructure/`, `interfaces/`, or `core/`.
2. `application/` may import from `domain/`.
3. `application/` must not import from `infrastructure/`.
4. `application/` must define ports as `Protocol` classes.
5. `infrastructure/` may import application ports and implement them.
6. `interfaces/http/` may import application use cases and HTTP schemas.
7. `interfaces/http/` should not import concrete infrastructure classes directly unless inside `dependencies.py`.
8. `main.py` is allowed to assemble the app and register routers.

## 5. Naming Rules

Use these names unless there is a strong domain-specific reason not to.

### Files

```text
application/ports.py
application/predict_text.py
infrastructure/model_loader.py
infrastructure/local_text_predictor.py
interfaces/http/routes.py
interfaces/http/schemas.py
interfaces/http/dependencies.py
```

### Classes

```text
PredictTextUseCase
TextPredictorPort
LocalTextPredictor
PredictTextRequest
PredictTextResponse
PredictionResult
PredictionError
```

### Methods and Functions

```text
execute()
predict()
load_model()
unload_model()
get_predict_text_use_case()
```

### Python Naming Style

- Files: `snake_case.py`
- Functions: `snake_case`
- Variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE`
- Exceptions: end with `Error`

## 5.1 No Performance Hardcoding

Do not hardcode values, branches, or special cases just to improve performance.

If performance needs improvement, use a general algorithmic change, profiling result, configuration, cache, index, batching, or data-structure improvement instead.

## 6. Canonical Flow

All prediction requests must follow this flow:

```text
HTTP request
→ interfaces/http/routes.py
→ interfaces/http/schemas.py validates request body
→ application/predict_text.py executes use case
→ application/ports.py defines required model interface
→ infrastructure/local_text_predictor.py performs actual model inference
→ application returns result
→ interfaces/http/routes.py returns response schema
```

Do not skip layers.

Bad:

```text
routes.py → torch model directly
routes.py → openai client directly
routes.py → database directly
application → infrastructure directly
```

Good:

```text
routes.py → PredictTextUseCase → TextPredictorPort → LocalTextPredictor
```

## 7. Required Code Pattern

### `application/ports.py`

```python
from typing import Protocol


class TextPredictorPort(Protocol):
    def predict(self, text: str) -> str:
        """Return prediction result for the given text."""
        ...
```

### `application/predict_text.py`

```python
from app.modules.prediction.application.ports import TextPredictorPort
from app.modules.prediction.domain.value_objects import InputText


class PredictTextUseCase:
    def __init__(self, predictor: TextPredictorPort) -> None:
        self._predictor = predictor

    def execute(self, text: str) -> str:
        input_text = InputText(value=text)
        return self._predictor.predict(input_text.value)
```

### `domain/value_objects.py`

```python
from dataclasses import dataclass

from app.modules.prediction.domain.exceptions import InvalidInputTextError


@dataclass(frozen=True)
class InputText:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise InvalidInputTextError("Input text must not be empty.")
```

### `domain/exceptions.py`

```python
class PredictionError(Exception):
    """Base exception for prediction domain errors."""


class InvalidInputTextError(PredictionError):
    """Raised when input text is invalid."""
```

### `infrastructure/local_text_predictor.py`

```python
from typing import Any

from app.modules.prediction.application.ports import TextPredictorPort


class LocalTextPredictor(TextPredictorPort):
    def __init__(self, model: Any) -> None:
        self._model = model

    def predict(self, text: str) -> str:
        return str(self._model.predict(text))
```

### `infrastructure/model_loader.py`

```python
from typing import Any


def load_model(model_path: str) -> Any:
    """Load a heavy AI model once at app startup."""
    # Replace this with actual model loading logic.
    raise NotImplementedError


def unload_model(model: Any) -> None:
    """Release model resources if needed."""
    del model
```

### `interfaces/http/schemas.py`

```python
from pydantic import BaseModel, Field


class PredictTextRequest(BaseModel):
    text: str = Field(..., min_length=1)


class PredictTextResponse(BaseModel):
    result: str
```

### `interfaces/http/dependencies.py`

```python
from fastapi import Request

from app.modules.prediction.application.predict_text import PredictTextUseCase
from app.modules.prediction.infrastructure.local_text_predictor import LocalTextPredictor


def get_predict_text_use_case(request: Request) -> PredictTextUseCase:
    predictor = LocalTextPredictor(model=request.app.state.prediction_model)
    return PredictTextUseCase(predictor=predictor)
```

### `interfaces/http/routes.py`

```python
from fastapi import APIRouter, Depends, HTTPException, status

from app.modules.prediction.application.predict_text import PredictTextUseCase
from app.modules.prediction.domain.exceptions import PredictionError
from app.modules.prediction.interfaces.http.dependencies import get_predict_text_use_case
from app.modules.prediction.interfaces.http.schemas import (
    PredictTextRequest,
    PredictTextResponse,
)

router = APIRouter(prefix="/predictions", tags=["predictions"])


@router.post("/text", response_model=PredictTextResponse)
def predict_text(
    request_body: PredictTextRequest,
    use_case: PredictTextUseCase = Depends(get_predict_text_use_case),
) -> PredictTextResponse:
    try:
        result = use_case.execute(text=request_body.text)
    except PredictionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return PredictTextResponse(result=result)
```

### `main.py`

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.modules.prediction.infrastructure.model_loader import load_model, unload_model
from app.modules.prediction.interfaces.http.routes import router as prediction_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.prediction_model = load_model(settings.model_path)
    yield
    unload_model(app.state.prediction_model)


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI FastAPI Service",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(prediction_router, prefix="/api/v1")
    return app


app = create_app()
```

## 8. AI Model Handling Rules

1. Heavy models must be loaded in `main.py` lifespan or a dedicated startup container.
2. Do not load models inside route functions.
3. Do not load models inside use case constructors unless the model is lightweight and explicitly approved.
4. Store long-lived model instances in `app.state` or a dependency container.
5. Wrap model calls behind an application port.
6. Keep model-specific preprocessing/postprocessing in `infrastructure/` unless it is a business rule.
7. If model output must be interpreted according to business rules, put that interpretation in `domain/` or `application/`.

## 9. FastAPI Rules

1. Use `APIRouter` per bounded context.
2. Route files must be thin.
3. Use Pydantic schemas only in `interfaces/http/schemas.py`.
4. Use `response_model` for all successful JSON endpoints.
5. Convert domain/application exceptions to `HTTPException` only in the HTTP layer.
6. Do not return raw model objects.
7. Do not expose internal error traces in API responses.

## 10. Sync and Async Rules

1. Use `def` routes for blocking local CPU/GPU inference.
2. Use `async def` only when the called libraries are awaitable.
3. Do not mark a route `async def` if it performs blocking model inference directly.
4. For external async clients, keep async logic in infrastructure adapters and expose an async port if needed.
5. Do not mix sync and async versions of the same use case unless required.

## 11. Configuration Rules

Use `app/core/config.py` for settings.

Required pattern:

```python
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "AI FastAPI Service"
    model_path: str = "models/model.pkl"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

Rules:

1. Do not hard-code secrets.
2. Do not commit real `.env` files.
3. Provide `.env.example` instead.
4. Access settings through `get_settings()`.

## 12. Docker Rules

Required files:

```text
Dockerfile
docker-compose.yml
.dockerignore
```

Dockerfile baseline:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi[standard] pydantic-settings

COPY ./app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`.dockerignore` baseline:

```text
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
.env
.git/
tests/
README.md
```

Rules:

1. Do not copy `.env` into the Docker image.
2. Do not copy test cache or virtual environments into the image.
3. Keep image dependencies explicit.
4. Use `docker-compose.yml` for local container execution.

## 13. Testing Rules

1. Test application use cases without FastAPI.
2. Mock application ports when testing use cases.
3. Test HTTP routes separately with FastAPI `TestClient`.
4. Do not load real heavy AI models in unit tests.
5. Use fake predictors for tests.

Example fake predictor:

```python
from app.modules.prediction.application.ports import TextPredictorPort


class FakeTextPredictor(TextPredictorPort):
    def predict(self, text: str) -> str:
        return f"fake:{text}"
```

## 14. Common Mistakes to Avoid

Do not generate this structure:

```text
app/
├─ services/
├─ models/
├─ routers/
└─ utils/
```

Reason: this structure hides domain boundaries and mixes business logic with technical implementation.

Do not put everything in `main.py`.

Do not put model loading in `routes.py`.

Do not place Pydantic HTTP schemas in `domain/`.

Do not place OpenAI, LangChain, PyTorch, Transformers, or vector DB code in `application/`.

Do not create vague files such as:

```text
utils.py
helper.py
common.py
service.py
manager.py
```

Use explicit names instead:

```text
predict_text.py
model_loader.py
local_text_predictor.py
prediction_repository.py
embedding_generator.py
```

## 15. When Adding a New Feature

For a new bounded context, create a new module:

```text
app/modules/{new_context}/
├─ domain/
├─ application/
├─ infrastructure/
└─ interfaces/http/
```

Examples:

```text
app/modules/chat/
app/modules/embedding/
app/modules/document_indexing/
app/modules/reranking/
```

Do not add unrelated features into `prediction/`.

## 16. Final Generation Checklist

Before generating or modifying code, verify:

- [ ] Is the code inside the correct bounded context?
- [ ] Is business logic outside FastAPI routes?
- [ ] Is AI SDK code only in `infrastructure/`?
- [ ] Is the use case inside `application/`?
- [ ] Are external dependencies accessed through ports?
- [ ] Is the model loaded once at startup?
- [ ] Are HTTP schemas separated from domain objects?
- [ ] Are filenames explicit and written in `snake_case`?
- [ ] Are class names explicit and written in `PascalCase`?
- [ ] Are tests using fake predictors instead of real heavy models?
