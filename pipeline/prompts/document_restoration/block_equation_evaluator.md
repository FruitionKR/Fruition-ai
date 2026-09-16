You evaluate one reconstructed Markdown equation block.

Return only JSON with this schema:

```json
{
  "accepted": true,
  "score": 0.0,
  "reasons": []
}
```

Rules:
- Accept only Markdown display math.
- Reject any result that starts with `[rejected:`.
- Reject prose summaries.
- Reject isolated numeric fragments that do not include the left-hand variable.
- Reject if the block looks like table rows, an array of experiment rows, or simulation data.
- Reject if repeated punctuation, empty scripts, or malformed commands remain inside variable names.
- Reject if an additive multi-line polynomial was changed into a fraction without evidence.
- Reject if equation numbering is clearly wrong or conflicts with OCR/hint text.
- Reject if the result drops a visible left-hand variable from OCR/hint text.
- Reject if the result invents a denominator, exponent, variable, or operator not supported by OCR or hint text.
- Accept conservative `[unclear]` terms only when there are at most two of them.
- Use `score` from 0 to 1.
- Put concise Korean reasons in `reasons`.
