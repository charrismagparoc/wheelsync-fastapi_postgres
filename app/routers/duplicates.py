from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..models import Profile, UserRole
from ..services.blacklist import customer_ids

# Customer accounts that share an ID number with another customer account. Blacklist records are checked
# separately (see /admin/customers/blacklist-matches). Flags accounts that are blacklisted by the admin.
router = APIRouter(prefix="/admin/customers", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/shared-ids")
def shared_ids(db: Session = Depends(get_db)):
    customers = db.scalars(select(Profile).where(Profile.role == UserRole.customer)).all()

    owners: dict[str, list[Profile]] = defaultdict(list)
    for customer in customers:
        for number in customer_ids(customer):
            owners[number].append(customer)

    result: dict[str, dict] = {}
    for group in owners.values():
        if len(group) < 2:
            continue
        for customer in group:
            entry = result.setdefault(
                str(customer.id),
                {"customer_id": str(customer.id), "shares_with": [], "blacklisted_share": False},
            )
            for other in group:
                if other.id == customer.id:
                    continue
                if any(item["customer_id"] == str(other.id) for item in entry["shares_with"]):
                    continue
                entry["shares_with"].append({
                    "customer_id": str(other.id),
                    "full_name": other.full_name,
                    "email": other.email,
                    "is_blacklisted": bool(other.is_blacklisted),
                })
                if other.is_blacklisted:
                    entry["blacklisted_share"] = True
    return list(result.values())
