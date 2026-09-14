"""Template picker: validation + fallback chain on OCR templates.

Adds to ``ocr_templates``:

* ``validate_enabled`` -- when set, processing OCRs ``validate_box`` on the page
  (with the template's image settings) and only uses the template if the read
  text contains ``validate_text``.
* ``validate_box``     -- JSON normalized ``[x0,y0,x1,y1]`` region to read.
* ``validate_text``    -- expected substring the read must contain.
* ``fail_template_id`` -- self-referential FK: the template to try next when
  validation fails.  Exhausting the chain stops with "No matched template
  found.".

All additive/nullable (``validate_enabled`` defaults to 0); existing templates
keep working unchanged.
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_template_validation"
down_revision = "0026_ocr_anchors"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ocr_templates",
                  sa.Column("validate_enabled", sa.Boolean(), nullable=False,
                            server_default=sa.text("0")))
    op.add_column("ocr_templates",
                  sa.Column("validate_box", sa.Text(), nullable=True))
    op.add_column("ocr_templates",
                  sa.Column("validate_text", sa.String(length=200),
                            nullable=True))
    op.add_column("ocr_templates",
                  sa.Column("fail_template_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_ocr_templates_fail_template", "ocr_templates",
                          "ocr_templates", ["fail_template_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_ocr_templates_fail_template", "ocr_templates",
                       type_="foreignkey")
    op.drop_column("ocr_templates", "fail_template_id")
    op.drop_column("ocr_templates", "validate_text")
    op.drop_column("ocr_templates", "validate_box")
    op.drop_column("ocr_templates", "validate_enabled")
