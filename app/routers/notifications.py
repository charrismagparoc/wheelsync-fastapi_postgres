from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, parse_uuid
from ..models import Notification, Profile
from ..schemas import NotificationOut
from ..realtime import hub

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(limit: int | None = Query(default=None, ge=1, le=200), user: Profile = Depends(get_current_user), db: Session = Depends(get_db)):
    stmt = select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at.desc())
    if limit:
        stmt = stmt.limit(limit)
    return db.scalars(stmt).all()


@router.get("/unread-count")
def unread_count(user: Profile = Depends(get_current_user), db: Session = Depends(get_db)):
    count = db.scalar(
        select(func.count()).select_from(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False))
    )
    return {"count": count or 0}


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(notification_id: str, user: Profile = Depends(get_current_user), db: Session = Depends(get_db)):
    n = db.get(Notification, parse_uuid(notification_id, "Notification"))
    if n is None or n.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    n.is_read = True
    db.commit()
    hub.publish("notifications", user.id)
    return n
