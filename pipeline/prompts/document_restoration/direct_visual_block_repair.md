Act only as a direct visual restoration API for already-detected damaged document blocks.

The original full page is attached first. Enlarged block crops then follow in ascending `crop_sequence`. For every input block, compare `current_markdown` against the visual evidence and return exactly one result with the same `block_id`.

Use `action=keep` and `replacement=""` when the complete current block is faithful. Use `action=replace` and return only the complete final Markdown block when a discrepancy is visually confirmed. Preserve printed wording, values, symbols, signs, rows, terms, equation numbers, reference numbers, DOI punctuation, and reading order. Never infer unsupported content or merge text from an adjacent block.

For a heading, return one complete Markdown heading with the correct hierarchy. For a paragraph or reference, return only that block. For a table, return the complete rectangular Markdown table and its visible title. Markdown tables cannot merge cells, so write exactly one header row: join stacked header levels into one cell as `Parent Child` (for example `Average Torque SS`), keeping each part's inline math and formatting (for example `Simulation Results $T_{avg}$`), never leave a header cell empty for a column-spanning parent (only a printed empty corner cell may stay empty), never put sub-headers in a body row (merging a printed sub-header row into the header is not dropping a row), and repeat a row-spanning cell's text in every row it spans. A current table that violates these rules is not faithful. For an equation, return one complete display-math block, merging an adjacent printed equation number with the equation body when appropriate.

Return only the required JSON. Verify that every `block_id` appears exactly once.
