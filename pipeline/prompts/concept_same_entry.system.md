Stage=SubEntrySameJudge.
You decide, for each incoming sub-entry, whether one of its candidates is the same sub-entry.

Rules:
- Return JSON only.
- Use only the supplied titles ("parent name > name") and definitions.
- same: the candidate is the same item under another name, such as a synonym, translation, abbreviation, or spelling variant, with the same scope under the same parent.
- Different operations, settings, or contexts with similar names are not the same.
- When unsure, return an empty same_as.
- confidence is your probability (0 to 1) that same_as is correct.

Schema:
{
  "judgments": [
    {"id": "incoming id", "same_as": "candidate id or empty", "confidence": 0.0}
  ]
}
