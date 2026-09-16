from dataclasses import replace

from app.modules.markdown_edit.application.ports import MarkdownEditorPort
from app.modules.markdown_edit.domain.entities import (
    MarkdownEditRequest,
    MarkdownEditResult,
    MarkdownEditTarget,
)
from app.modules.markdown_edit.domain.markdown_target_scope import (
    markdown_line_count,
    markdown_line_range,
)


class GenerateMarkdownEditUseCase:
    def __init__(self, editor: MarkdownEditorPort) -> None:
        self._editor = editor

    def execute(self, request: MarkdownEditRequest) -> MarkdownEditResult:
        if not request.instruction.strip():
            raise ValueError("Edit instruction is required.")
        if not request.markdown.strip():
            raise ValueError("Markdown is required.")
        if request.target.start_line < 1:
            raise ValueError("target.start_line must be greater than 0.")
        if request.target.end_line < request.target.start_line:
            raise ValueError("target.end_line must be greater than or equal to target.start_line.")
        line_count = markdown_line_count(request.markdown)
        if request.target.end_line > line_count:
            raise ValueError("target.end_line must not exceed the Markdown line count.")
        if request.target.type == "whole_document" and (
            request.target.start_line != 1 or request.target.end_line != line_count
        ):
            raise ValueError("whole_document target must cover the entire Markdown document.")
        if (
            request.edit_operation == "insert_after"
            and request.edit_destination == "target"
            and request.target.type != "current_section"
        ):
            raise ValueError("insert_after operation requires a current_section target.")
        if request.edit_operation == "replace" and request.edit_destination != "target":
            raise ValueError("replace operation requires the requested target destination.")

        editor_request = request
        if request.edit_destination == "document_end":
            editor_request = replace(
                request,
                target=MarkdownEditTarget(
                    type="whole_document",
                    start_line=1,
                    end_line=line_count,
                ),
            )
        result = self._editor.generate_edit(editor_request)
        operation = result.edit.operation
        if operation != request.edit_operation:
            raise ValueError(f"Edit operation must be {request.edit_operation}.")
        actual_target = result.edit.actual_target
        if actual_target.start_line < 1:
            raise ValueError("actual_target.start_line must be greater than 0.")
        if actual_target.end_line < actual_target.start_line:
            raise ValueError("actual_target.end_line must be greater than or equal to actual_target.start_line.")
        if actual_target.end_line > line_count:
            raise ValueError("actual_target.end_line must not exceed the Markdown line count.")
        appends_to_document = (
            operation == "insert_after"
            and actual_target.type == "whole_document"
            and request.edit_destination == "document_end"
        )
        if (
            not appends_to_document
            and (
                actual_target.start_line > request.target.start_line
                or actual_target.end_line < request.target.end_line
            )
        ):
            raise ValueError("actual_target must contain the requested target.")
        if actual_target.type == "whole_document" and (
            actual_target.start_line != 1 or actual_target.end_line != line_count
        ):
            raise ValueError("whole_document actual_target must cover the entire Markdown document.")
        if operation == "insert_after":
            allowed_target_types = {
                "whole_document"
                if request.edit_destination == "document_end"
                else "current_section"
            }
            if actual_target.type not in allowed_target_types:
                expected = " or ".join(sorted(allowed_target_types))
                raise ValueError(f"insert_after operation requires a {expected} actual_target.")
            if actual_target.type == "current_section" and request.target.type != "current_section":
                raise ValueError("insert_after current_section operation requires a current_section target.")
        if not result.edit.replacement_markdown.strip():
            raise ValueError("replacement_markdown must not be empty.")

        actual_markdown = markdown_line_range(
            request.markdown,
            actual_target.start_line,
            actual_target.end_line,
        )
        changed = operation == "insert_after" or result.edit.replacement_markdown != actual_markdown
        return replace(
            result,
            edit=replace(
                result.edit,
                requested_target=request.target,
                changed=changed,
            ),
        )
