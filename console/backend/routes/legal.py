"""Public project legal information, with no operator or workspace data."""
from fastapi import APIRouter

from chatcopilot.project import legal_info

router = APIRouter()


@router.get("/api/legal")
def read_legal_info():
    return legal_info()
