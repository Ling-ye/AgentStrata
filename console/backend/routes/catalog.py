from __future__ import annotations

from fastapi import APIRouter, HTTPException

from console.control import catalog

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("")
def list_catalog():
    return [item.to_dict() for item in catalog.full_catalog()]


@router.get("/{item_id:path}")
def get_catalog_item(item_id: str):
    item = catalog.catalog_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"catalog item not found: {item_id}")
    return item.to_dict()
