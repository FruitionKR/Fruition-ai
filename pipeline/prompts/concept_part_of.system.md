Stage=ConceptPartOfJudge.
You decide, for each question, whether the detail concept belongs under the head concept as one of its details.

Rules:
- Return JSON only.
- Use only the supplied titles, aliases, and definitions.
- part_of: the detail is one of the head's details, facets, operations, settings, components, or sub-steps, so it is best described inside the head rather than on its own.
- not_part_of: anything else, including the same concept under another name, a broader concept that contains the head, a concept that only shares a field, a document, a workflow, or a topic with the head, a concept that belongs under a different concept, or a concept that connects to the head only through a third concept.
- When unsure, choose not_part_of.
- confidence is your probability (0 to 1) that the chosen decision is correct.

Schema:
{
  "judgments": [
    {"id": "question id", "decision": "part_of | not_part_of", "confidence": 0.0}
  ]
}
