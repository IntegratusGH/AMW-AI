"""users: add is_ai_trainer flag

Revision ID: 0012_user_ai_trainer
Revises: 0011_cr_formats
Create Date: 2026-09-09

Adds an AI Trainer flag to users. Only AI Trainers may define/train new CR-book
formats; everyone else is shown the "not trained" message. Existing
administrators are made AI Trainers so format training is not locked out.
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_user_ai_trainer"
down_revision = "0011_cr_formats"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("is_ai_trainer", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.execute(
        "UPDATE users SET is_ai_trainer = 1 "
        "WHERE role_id IN (SELECT id FROM roles WHERE name = 'Administrator')"
    )


def downgrade():
    op.drop_column("users", "is_ai_trainer")
