# PDF cost preflight (internal)

`POST /internal/documents/pdf-cost-features`, authenticated with `X-Internal-Token`, accepts
`{"source_key":"sources/documents/<document-id>/original"}`. Document-svc authorizes the
source before calling. The endpoint downloads the PDF into a temporary directory and returns
`pages`, `text_bytes` (UTF-8), `images`, and `scanned_pages` (fewer than 40 extracted text characters).
It makes no provider calls. Encrypted, empty, or unreadable PDFs return 422; storage failures
remain errors, never a zero-cost estimate. Deploy this endpoint before document-svc's cost preflight.
