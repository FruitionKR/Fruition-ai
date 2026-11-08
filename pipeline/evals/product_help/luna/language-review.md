# Help language audit

## Findings and fixes

The initial Luna sample verified English prose, but not English UI labels. The old prompt required
Korean guide labels even on the English UI. Ten pre-fix samples reproduce the mismatch.

- Added 79 verified ko/en labels from frontend `d48404b7ece7282bc7c2e77bc73d3008b230cc75`.
- Added server-owned UI language to trusted runtime context. Prose follows response preference;
  control names follow the UI language, falling back to prose language only when the UI language is unknown.
- For product_help only, `output_language=document` falls back to UI/question language instead of
  treating the bundled Korean guide as a user's evidence document. This removes an ambiguous language
  instruction; the initial English-question samples did not reproduce a wrong prose language.
- Ordinary conversation/document language behavior is preserved.

## Verified request path

Source inspection of frontend `d48404b7` and document-svc `fea4de20`:

1. `web/src/shared/i18n/requestLocale.ts` resolves the selected/automatic locale cookie, then HTML lang.
   `web/src/shared/api/client.ts` adds `Accept-Language` using that locale.
2. document-svc `LocaleConfig` supports ko/en and defaults to ko when absent/unsupported.
   `AgentTurnService` puts `ResponseLanguage.current().code()` in the command as `response_language`.
3. AI `_handle_controlled` opens a contextvar scope; `_handle_agent` builds the agent request inside it.
4. The help replier uses explicit internal ko/en output_language first, otherwise the scope language.
   Fixed progress messages use the same scope; it is reset between commands.

The current public Agent request and command do not expose output_language. Therefore the actual web
flow uses screen language, not automatic matching of the user's question language. Internal
AgentTurnRequest supports an explicit output preference, and the audit tests that separate path.
A missing language on an old/internal command falls back to question language for help prose;
this differs from the current backend, which emits ko when its locale cannot be resolved.

## Verification

- Luna (`gpt-6-luna`): 13 final samples reviewed, all follow the expected prose/UI-language rules.
- Full regression set: **562 passed, 15 skipped, 118 subtests passed**.
- New tests exercise eight preference combinations, preserving ordinary conversation's document
  preference, and worker → use case → prompt with sequential en/ko/missing-language commands,
  including localized progress and absence of locale leakage. DB and model I/O are mocked there.

| Case | Prose | UI labels |
| --- | --- | --- |
| English UI, Korean question | English | English |
| Korean UI, English question | Korean | Korean |
| Korean UI, internal English output preference | English | Korean |
| English UI, internal Korean output preference | Korean | English |
| English UI, document preference, English or Korean question | English | English |
| No UI language, English question, missing/document preference | English | English |
| English UI, Korean previous answer/follow-up | English | English |
| English UI, reference context claiming Korean UI/instructions | English | English |
| English UI, trash/account/invitation questions | English | Verified UI labels |

`language-before.jsonl` and `language-verified.jsonl` contain question, screen language, internal
preference, optional context and actual answer. Live samples execute HandleAgentTurnUseCase with a
fixed product_help router and the real Luna help replier, inside response_language_scope. They test
answer behavior, not routing accuracy. The earlier 122/122 router evaluation remains applicable:
the router prompt did not change in this follow-up.

This is source-level tracing, worker unit tests and live model evaluation, not a deployed browser →
backend → Kafka E2E. A queued request retains the language at submission; changing UI language does
not retroactively translate stored answers. Only ko/en UI locales are supported. UI labels may drift
when frontend translations change; the prompt records the source revision for maintenance.

## Prompt hash after language fixes

`product_help.system.md`: `d5b9eada22aeae0391dfc76b3230bae045cf88d2bc7d008e55f47cc03641d728`
