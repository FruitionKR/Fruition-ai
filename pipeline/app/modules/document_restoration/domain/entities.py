from enum import Enum


class RestorationMode(str, Enum):
    CROP_FIRST = "crop-first"
    DOCLING_ONLY = "docling-only"
    SELECTIVE_REPAIR = "selective-repair"
    FULL_REPAIR = "full-repair"


class RestorationStage(str, Enum):
    PREPARE_CROP_FIRST = "prepare_crop_first_with_anydoc"
    ASSEMBLE_CROP_FIRST = "assemble_crop_first_markdown"
    DOCLING_BASELINE = "docling_baseline"
    PUBLISH_DOCLING_MARKDOWN = "publish_docling_markdown"
    DETECT_LAYOUT_BLOCKS = "detect_layout_blocks"
    DETECT_EQUATION_CANDIDATES = "detect_docling_equation_candidates"
    BUILD_PRIMARY_MANIFEST = "build_docling_primary_manifest"
    AUGMENT_TEXT_CANDIDATES = "augment_text_candidates_with_crop_ocr"
    ASSEMBLE_DETECTED_MARKDOWN = "assemble_detected_markdown"
    SELECTIVE_REPAIR_WITH_PROVIDER = "selective_repair_with_provider"
    RECOVER_BLOCKS = "recover_blocks"
    REVIEW_BLOCKS_WITH_VISION = "review_blocks_with_vision"
    RECOVER_FIGURES_WITH_VISION = "recover_figure_blocks_with_vision"
    ASSEMBLE_MARKDOWN = "process_auto_layout_blocks"
