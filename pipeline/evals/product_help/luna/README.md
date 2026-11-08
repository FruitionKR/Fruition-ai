# Product help — Luna evaluation

- Model: `openai / gpt-6-luna`; existing runtime router/replier builders and default model settings.
- Base: `origin/dev`, `d6be13e2dc5ed9ee7e88fa57ba474cfd36825d3e`.
- Routing dataset: `pipeline/evals/agent_turn_router.jsonl` (37 existing + 24 product-help cases).
- Four concurrent requests; each row records wall time including contract retries.
- Credentials were loaded only into process environment; no credentials are included here.

## Results

| Evaluation | Result |
| --- | --- |
| Original dev prompt, existing cases, one pass | 37/37 |
| Initial help prompt | 60/61 |
| Second router prompt | 60/61 |
| Final router prompt, full dataset twice | **122/122** |
| Final existing cases | **74/74** |
| Final new help/boundary cases | **48/48** |
| False help routing in final runs | **0** |
| Final route contract failures | **0** (runtime retry remains enabled) |
| Help answers, direct executor samples | **14/14 reviewed** |
| Unit/regression checks | **552 passed, 15 skipped, 118 subtests passed** |

The first router mismatch searched workspace evidence for an app already described in conversation.
The second run routed a generic document-editing question to help without a resolved Fruition subject.
The final prompt explicitly preserves conversation-only explanations and requires a resolved product subject.
Expected routes were not relaxed to make these failures pass. Earlier results are retained here.

Answer review found an unsupported “No” before saying body-search support was unknown, and inferred
parent menus for AI usage/member settings. The final help prompt forbids these extrapolations.
The 14 final answers were reviewed against the supplied screen guide for UI steps, limits, uncertainty,
no claimed execution, follow-up continuity, language, and ignoring a fake guide in reference_context.
`answers.jsonl` contains final outputs and the two nonempty contexts. This is manual review, not an
independent model judge or a guarantee that all possible questions are covered.

## Timing (seconds)

| Sample | Median | Maximum |
| --- | --- | --- |
| Dev prompt, existing cases (37) | 2.376 | 5.075 |
| Final prompt, existing cases (74) | 2.444 | 5.730 |
| Final prompt, new cases (48) | 2.300 | 5.795 |
| Help answer generation only (14) | 2.751 | 3.990 |

Existing-case median increased by about 0.07 seconds in this sample. Runs were not randomized,
cache-controlled, or load-controlled, so this is diagnostic timing, not a latency regression guarantee.
The help answer timing excludes routing. No extra classifier call was introduced.

## Reproduce and limitations

With `OPENAI_API_KEY` set, run from `pipeline`:

```sh
python evaluate_agent_turn_router.py --provider openai --model gpt-6-luna
python -m pytest tests/modules/agent tests/modules/query/test_query_event.py tests/test_openapi_snapshot.py tests/workers tests/core/test_response_language.py -q
```

The CLI evaluates once serially; these recorded routing runs used the same `evaluate_cases` function
with one case per worker and four workers. For the baseline, only the router system prompt was replaced
with `git show origin/dev:pipeline/prompts/agent_turn_router.system.md`; validation/client code was shared.
Repeat the final dataset twice to match the final sample count. Answer samples use
`build_conversation_replier(provider="openai", model="gpt-6-luna", prompt_path=DEFAULT_PRODUCT_HELP_PROMPT)`
and `AgentTurnRequest` with the question, output_language and context in `answers.jsonl`.

Live Jev routing, frontend rendering of the new action, deployed UI consistency, and production load
were not tested. Jev option/contract integration is covered by unit tests. No deployment was performed.

## Final prompt hashes (SHA-256)

- `agent_turn_router.system.md`: `62a18b515d8a25ac3576b3b6ca9b5fddaaa0885e4c0531fb247863b6eaeb26de`
- `product_help.system.md`: `ac293cdd9dcabe8392b7072c05a9750d18086ab3e8630adc01f378341de05fd3`
