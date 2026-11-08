# Product help integration verification — 2026-11-08

The tested source starts from the fetched `origin/dev` of all three repositories:

| Repository | dev | Implementation |
| --- | --- | --- |
| AI | d6be13e | 850fccd, including unmerged PR #245 |
| Frontend | a0a56dd5 | 4257c2f2 |
| Document | fea4de2 | 44cd9bb |

## Scope

- Real Chromium UI submission → document API/outbox → isolated Kafka topics → Python workers → OpenAI GPT-6 Luna → Kafka result → PostgreSQL → public message API → rendered answer.
- Korean and English, chat and task tabs: four product-help cases. Each answered with Settings → AI settings → Chat → Turn on web search by default, using localized labels.
- Korean and English ambiguous app questions: two cases with empty conversation history. Each asked whether the user meant Fruition or another app.
- Every case checked visibility before and after a full page reload. Task results remained in the task tab with `action=product_help`.
- Receipt checks verified `status=succeeded`, the request's `response_language`, and `model=gpt-6-luna`; task projections were `ready`. No documents were created or updated after the first help request; initial empty notes created by the UI predated the requests.
- Eight additional real-model routing checks covered help, ambiguous app references, the user's own app, follow-up questions, and a new document request after help.

## Environment and limits

The test used dedicated PostgreSQL/Redis instances, isolated Kafka topics, synthetic accounts/workspaces, and the modified frontend, Python workers/API, and document classes. No model, transport, or result-storage mocks were used. The access service was the existing local image and was used for fixture signup/authentication only. Test email verification was provisioned in the isolated DB.

The document service ran from compiled classes with the already-resolved Byte Buddy runtime jar added explicitly: the local Gradle bootJar/runtime classpath omitted that transitive jar. This is application-path E2E evidence, not validation of a production deployment image. Jev routing, other providers, voice output, and production load were not exercised.

The frontend's `web/e2e/productHelp.mjs` is the reusable browser runner. Start each run with an empty dedicated workspace; the product's 10-session limit and prior conversation history must not contaminate independent cases. Credentials and browser storage are excluded from the recorded artifacts.

## Recorded results

- [Browser/API/receipt results](browser.json): **6/6 passed**.
- [Actual classifier outputs](routing.json): **8/8 passed**.
- [Browser runner and setup](https://github.com/FruitionKR/Fruition-frontend/tree/fix/246-product-help-integration/web/e2e).
- [Reload screenshots](https://github.com/FruitionKR/Fruition-frontend/tree/fix/246-product-help-integration/docs/e2e/product-help).

With runtime provider credentials supplied in the process environment, rerun the routing checks from `pipeline/`:

```sh
PYTHONPATH=. python evals/product_help/integration/check_routing.py
```

Results are written to `/tmp/product-help-routing.json`. The script makes real model calls and asserts all eight expected routes.
