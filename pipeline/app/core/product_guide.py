"""Official public guide shared by product-help answers and MCP readers."""
from pathlib import Path

PRODUCT_GUIDE_PATH = Path(__file__).resolve().parents[2] / "prompts" / "product_guide.md"


def load_product_guide() -> str:
    return PRODUCT_GUIDE_PATH.read_text(encoding="utf-8")
