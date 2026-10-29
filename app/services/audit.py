"""Audit log: one row per admin action, written after the action succeeded."""
import logging

from sqlalchemy.orm import Session

from ..models import AuditLog, Profile

logger = logging.getLogger(__name__)


def record(db: Session, actor: Profile, action: str, summary: str, *, entity: str | None = None, entity_id=None) -> None:
    try:
        db.add(AuditLog(
            actor_id=actor.id, actor_name=actor.full_name, actor_role=actor.role.value,
            action=action, entity=entity, entity_id=str(entity_id) if entity_id is not None else None,
            summary=summary[:1000],
        ))
        db.commit()
    except Exception:  # the action itself already succeeded; never fail the request over its log line
        db.rollback()
        logger.exception("Could not write audit log entry for %s", action)
