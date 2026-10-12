"""Create (or promote) an admin account from the command line.

    python -m scripts.create_admin "Juan Dela Cruz" admin@pardzcarrental.com 's3cret-pass'
"""
import sys

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Profile, UserRole
from app.security import hash_password


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit('usage: python -m scripts.create_admin "Full Name" email password')
    full_name, email, password = sys.argv[1], sys.argv[2].strip().lower(), sys.argv[3]
    if len(password) < 6:
        sys.exit("Password must be at least 6 characters.")
    init_db()
    with SessionLocal() as db:
        user = db.scalar(select(Profile).where(Profile.email == email))
        if user:
            user.role = UserRole.admin
            user.password_hash = hash_password(password)
            print(f"Updated existing user {email} -> admin")
        else:
            db.add(Profile(full_name=full_name, email=email, password_hash=hash_password(password), role=UserRole.admin))
            print(f"Created admin {email}")
        db.commit()


if __name__ == "__main__":
    main()
