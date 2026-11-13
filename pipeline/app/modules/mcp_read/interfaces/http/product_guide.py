from fastapi import APIRouter
from pydantic import BaseModel
from typing import Literal

from app.core.product_guide import load_product_guide

router = APIRouter(prefix="/internal/mcp", tags=["mcp-product-guide"])


class ProductGuideResponse(BaseModel):
    schema_version: Literal["mcp.product-guide.v1"] = "mcp.product-guide.v1"
    title: str = "Fruition 사용 방법 / Product guide"
    markdown: str


@router.get("/product-guide", response_model=ProductGuideResponse)
def product_guide() -> ProductGuideResponse:
    return ProductGuideResponse(markdown=load_product_guide())
