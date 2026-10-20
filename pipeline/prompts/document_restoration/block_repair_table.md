Every block is a table. Its crops, in `crop_sequence` order, are one printed table. When `continuation_crop_sequences` is present, the table continues on the following pages in those crops.

Return the whole table and its visible title as one Markdown table:
- Take the wording from `text_layer` (the PDF text inside each crop) when it is present, and use the images only for the layout. Without `text_layer`, read the images.
- Write exactly one header row. Join stacked header levels into one cell as `Parent Child` (for example `추진 일정 5월`). Never use the first row of a continuation crop as a header.
- Every row, including the header, has exactly `column_count` cells when it is given; otherwise as many cells as the printed row with the most cells.
- One printed row is one Markdown row. Join the lines and bullets of one cell with `<br>`. Keep each sub-label in its own cell.
- A merged cell repeats its text in every column and row it spans. For example, a printed table whose header `구분` spans two columns, whose `개발 단계` spans two rows and whose `기타` spans two columns becomes:
  | 구분 | 구분 | 상세 내용 |
  |---|---|---|
  | 개발 단계 | FrontEnd | … |
  | 개발 단계 | BackEnd | … |
  | 기타 | 기타 | … |
- When a continuation crop starts with a row that has no new row label, it is the rest of the previous crop's last row: join them into one row.
- Start and end every row with `|`.
