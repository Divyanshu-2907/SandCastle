"""Insert a known user + task so the schema can be poked at by hand.

Idempotent: re-running updates the existing rows instead of duplicating them.

    docker compose exec backend python scripts/seed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models import Run, RunStatus, Task, User  # noqa: E402

SEED_EMAIL = "demo@sandcastle.dev"
SEED_PASSWORD = "sandcastle"
SEED_REPO = "https://github.com/octocat/Hello-World.git"
SEED_DESCRIPTION = "Add a build-status badge to the top of README.md."


def main() -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == SEED_EMAIL))
        if user is None:
            user = User(email=SEED_EMAIL, hashed_password=hash_password(SEED_PASSWORD))
            db.add(user)
            db.flush()
            print(f"created user   {user.id}  {user.email}")
        else:
            print(f"user exists    {user.id}  {user.email}")

        task = db.scalar(
            select(Task).where(Task.user_id == user.id, Task.description == SEED_DESCRIPTION)
        )
        if task is None:
            task = Task(user_id=user.id, repo_url=SEED_REPO, description=SEED_DESCRIPTION)
            db.add(task)
            db.flush()
            print(f"created task   {task.id}")
        else:
            print(f"task exists    {task.id}")

        # A queued run gives the run/step/diff/score tables something to hang off.
        run = db.scalar(select(Run).where(Run.task_id == task.id))
        if run is None:
            run = Run(task_id=task.id, agent_model=settings.agent_model, max_steps=15)
            db.add(run)
            db.flush()
            print(f"created run    {run.id}  status={RunStatus(run.status).value}")
        else:
            print(f"run exists     {run.id}")

        db.commit()

    print(f"\nlogin with:  {SEED_EMAIL} / {SEED_PASSWORD}")


if __name__ == "__main__":
    main()
