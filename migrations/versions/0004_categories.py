"""module categories

Revision ID: 0004_categories
Revises: 0003_modules
Create Date: 2026-09-08

Introduces a ``categories`` table and replaces the free-text ``modules.category``
column with a nullable ``category_id`` foreign key (ON DELETE SET NULL).
Existing category strings are migrated into rows and back-filled onto modules.
"""
import datetime as dt

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_categories"
down_revision = "0003_modules"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_categories_name"), "categories", ["name"], unique=True)

    op.add_column("modules", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_modules_category_id"), "modules", ["category_id"], unique=False
    )
    op.create_foreign_key(
        "fk_modules_category_id_categories", "modules", "categories",
        ["category_id"], ["id"], ondelete="SET NULL",
    )

    # Migrate existing free-text categories into rows, then back-fill the FK.
    conn = op.get_bind()
    names = [
        r[0] for r in conn.execute(sa.text(
            "SELECT DISTINCT category FROM modules "
            "WHERE category IS NOT NULL AND LTRIM(RTRIM(category)) <> ''"
        ))
    ]
    now = dt.datetime.utcnow()
    for i, name in enumerate(sorted(names)):
        conn.execute(
            sa.text(
                "INSERT INTO categories (name, is_active, sort_order, "
                "created_at, updated_at) VALUES (:n, :a, :s, :c, :u)"
            ),
            {"n": name, "a": True, "s": i, "c": now, "u": now},
        )
    conn.execute(sa.text(
        "UPDATE modules SET category_id = "
        "(SELECT id FROM categories WHERE categories.name = modules.category) "
        "WHERE category IS NOT NULL"
    ))

    op.drop_column("modules", "category")


def downgrade():
    op.add_column("modules", sa.Column("category", sa.String(length=80), nullable=True))
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE modules SET category = "
        "(SELECT name FROM categories WHERE categories.id = modules.category_id) "
        "WHERE category_id IS NOT NULL"
    ))
    op.drop_constraint(
        "fk_modules_category_id_categories", "modules", type_="foreignkey"
    )
    op.drop_index(op.f("ix_modules_category_id"), table_name="modules")
    op.drop_column("modules", "category_id")
    op.drop_index(op.f("ix_categories_name"), table_name="categories")
    op.drop_table("categories")
