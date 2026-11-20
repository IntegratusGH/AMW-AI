"""Template picker: optional second validate box on OCR templates.

Adds to ``ocr_templates``:

* ``validate_box2``  -- JSON normalized ``[x0,y0,x1,y1]`` region to read.
* ``validate_text2`` -- expected substring/pattern the second read must match.

When both ``validate_box``/``validate_text`` and ``validate_box2``/
``validate_text2`` are configured, BOTH must match for the template to be used
(logical AND); otherwise processing falls back to ``fail_template_id``.  Both
columns are additive/nullable; existing templates keep working with a single
validate box unchanged.
"""
import sqlalchemy as sa
from alembic import op

revision = "0039_template_validation_box2"
down_revision = "0038_ocr_text_anchors"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ocr_templates",
                  sa.Column("validate_box2", sa.Text(), nullable=True))
    op.add_column("ocr_templates",
                  sa.Column("validate_text2", sa.String(length=200),
                            nullable=True))


def downgrade():
    op.drop_column("ocr_templates", "validate_text2")
    op.drop_column("ocr_templates", "validate_box2")
