# Python Refactoring Rules

## Purpose

Use this document as the default rule set for Python refactoring work.
This is not a plan for a specific module. It defines how an agent should judge and execute Python refactoring when the user asks for it.

Follow `docs/python_convention.md` for repository architecture and layer rules.
Use this document for refactoring-specific decisions on top of that convention.

## Core Principles

1. Preserve behavior first.
2. Do not change external contracts unless explicitly requested.
3. Keep each refactoring step small enough to verify.
4. Do not introduce abstractions without real duplicated behavior or real complexity reduction.
5. Prefer simple Python code over pattern-heavy designs.
6. Do not mechanically port Java/Spring class patterns into Python.
7. Make boundaries clearer: pure logic, I/O, orchestration, and external clients should be distinguishable.

## Pre-Refactoring Checks

Before changing code, inspect:

- Public API or HTTP response contracts.
- DB schema and migration impact.
- File, object storage, and external I/O impact.
- Environment variables and runtime settings.
- Existing tests and gaps.
- Whether the code is called by CLI, API, background tasks, or tests.
- Current dirty worktree state.

Do not move code before these are understood.

## Success Criteria

A refactoring is successful when:

- Existing tests still pass.
- External behavior is preserved.
- Responsibilities are easier to name.
- I/O and pure logic are more clearly separated.
- New abstractions make the next change smaller.
- Call flow remains understandable.

Do not treat “the code looks cleaner” as sufficient proof.

## Function Rules

Prefer functions that:

- Have clear inputs and outputs.
- Do one coherent job.
- Avoid hidden side effects.
- Propagate meaningful errors.
- Can be tested without real DB, network, filesystem, or LLM calls.

Avoid functions that:

- Parse, validate, call LLMs, write DB rows, and assemble responses in one body.
- Use flags to switch between unrelated behaviors.
- Return different shapes depending on branch.
- Mutate `dict[str, Any]` across many stages.
- Require tests to know too much about internal state.

### Guard Clause and Function Extraction Rules

- Keep `return` and `continue` guard clauses when they make rejection or skip conditions explicit and keep the main path flat.
- Do not extract a function only because several places use the same `if`, `return`, or `continue` shape.
- Extract shared logic when the repeated code has the same input contract, validation conditions, failure meaning, and output shape.
- Preserve validation order, short-circuit behavior, and the distinction between skipping an item and rejecting the whole operation.
- Name extracted functions after their domain responsibility, not generic mechanics such as `handle_condition` or `process_items`.
- Add focused tests for extracted pure logic and rerun every affected caller's tests.

## Class Rules

Use classes only when state, dependency injection, or a clear role justifies them.

Preferred roles:

- `UseCase`: orchestration.
- `Repository`: persistence.
- `Client`: external API or service call.
- `Assembler` / `Builder`: data assembly.
- `Evaluator` / `Scorer`: judgment or scoring policy.

Avoid:

- Large generic `Service` classes.
- Classes that own every dependency and perform every step.
- Stateless namespace classes.
- Inheritance for small behavior differences.
- Classes that are hard to replace with fakes in tests.

## Data Structure Rules

Use explicit types for repeated internal structures.

Prefer:

- `dataclass` for domain/application data.
- `Protocol` for application ports.
- Pydantic models only at HTTP boundaries.
- `dict[str, Any]` only for raw external JSON, LLM raw output, migrations, or narrow adapter code.

Avoid:

- Repeating the same dict keys across unrelated modules.
- Passing raw LLM JSON through the whole application.
- Treating DB row dicts as domain models.
- Optional-heavy structures that obscure meaning.

## I/O Boundary Rules

Treat filesystem, DB, object storage, HTTP, web search, and LLM calls as boundaries.

Prefer:

- Thin I/O adapters.
- Conversion from I/O results into application data before business logic.
- Pure logic that can be tested with strings, dataclasses, and lists.
- Product flows based on DB/object storage, not temporary artifacts.
- Debug artifacts only behind explicit options.

Avoid:

- Creating temporary files in the middle of business logic and reading them back.
- Requiring JSON/Markdown artifact paths for DB persistence.
- Mixing LLM calls with business policy.
- Tests that require real external APIs, real DB, or real object storage.

## Error Handling Rules

Errors must not hide their cause.

Prefer:

- Convert boundary exceptions into meaningful application/domain errors.
- Separate user-facing messages from internal debug data.
- Use fallback only when it is an intentional policy.
- Emit event/log data when execution continues after a failed optional step.

Avoid:

- Broad `except Exception: pass`.
- Turning failures into empty lists or empty strings silently.
- Swallowing errors to make tests pass.
- Treating unsupported answers and system failures as the same state.

## Naming Rules

Names should describe domain responsibility, not implementation mechanics.

Prefer names like:

- `build_query_context`
- `answer_query`
- `query_answer_evaluator`
- `wiki_ingestion_repository`
- `source_blocks`
- `evidence_snippets`

Avoid overusing:

- `manager`
- `handler`
- `processor`
- `helper`
- `utils`

Do not use names that overclaim behavior. For example, do not use `persist` for code that only builds data and does not store it.

## Layer Rules

`docs/python_convention.md` is the authority for layer rules.

Additional refactoring guidance:

- Keep `interfaces/http` limited to request/response conversion and dependency wiring.
- Keep `application` focused on use case flow and port calls.
- Keep `domain` pure.
- Keep DB, object storage, LLM, web search, and filesystem code in `infrastructure`.
- Move oversized CLI orchestration into application use cases when it blocks testing or reuse.

## LLM Pipeline Refactoring Boundaries

Keep these responsibilities distinct:

- Ingest orchestration.
- Semantic extraction.
- Source/concept assembly.
- Persistence.
- Query retrieval.
- Evidence selection.
- Answer generation.
- Evaluator/web augmentation.
- Citation post-processing.

Do not substantially rewrite several of these boundaries in one refactoring unless explicitly requested.

## Query Refactoring Contracts

Do not break these contracts:

- Preserve the distinction between original user question and retrieval question.
- Multi-turn context is passed through `recent_conversation_summary` and `reference_context`.
- Source and concept pages can both be evidence candidates.
- Web evidence must remain distinguishable from internal evidence.
- Citations must connect to evidence actually used in the answer.
- Evidence without answer citations must not be presented as used evidence.

## Ingest Refactoring Contracts

Do not break these contracts:

- The default product path must not depend on intermediate JSON/Markdown files.
- Source/concept Markdown should flow directly to DB/object storage.
- Source blocks must remain queryable by `source_document_id + block_id`.
- `wiki_embedding_vectors` reuses canonical representations.
- `wiki_embedding_units` links pages to source block evidence.
- Debug artifacts are written only behind explicit options.

## Test Rules

Before and after refactoring:

- Run relevant existing tests.
- Add characterization tests before risky movement.
- Add focused tests for newly extracted pure logic.
- Use fakes or in-memory implementations for external systems.
- If a bug fix is mixed in, add the reproducing test first.

Avoid:

- Large file moves without tests.
- Mocks that overfit private implementation details.
- Meaningless tests added only for coverage.
- Snapshot-only verification without behavioral assertions.

## Step Order

Prefer this order:

1. Add tests or characterization coverage.
2. Introduce explicit types, dataclasses, or Protocols.
3. Extract pure logic.
4. Separate I/O boundaries.
5. Clean up application use cases.
6. Clean up HTTP or CLI wiring.
7. Remove orphans created by the refactor.

Each step should be independently verifiable.

## Do Not

- Do not mix large feature changes with refactoring.
- Do not revert user changes.
- Do not move files based on preference alone.
- Do not add one-use abstractions.
- Do not add configurability without a current need.
- Do not add classes for speculative future flexibility.
- Do not convert dicts to classes without clarifying responsibility.

## Agent Checklist

When asked to refactor Python code:

1. Read the relevant code first.
2. Confirm external contracts and tests.
3. Define the refactoring goal in one sentence.
4. State what will not change.
5. Split the work into small steps.
6. Define verification for each step.
7. Explain edit scope and impact before modifying files.
8. Modify only after approval.
9. Run tests.
10. Report remaining risks and next refactoring candidates.
