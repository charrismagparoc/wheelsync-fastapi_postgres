from fastapi import APIRouter

from ..services.agreement import RENTAL_AGREEMENT_TERMS

router = APIRouter(prefix="/public", tags=["public information"])


@router.get("/rental-agreement")
def rental_agreement_terms():
    return RENTAL_AGREEMENT_TERMS