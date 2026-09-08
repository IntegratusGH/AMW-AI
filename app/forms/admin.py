"""Administrative forms for user management."""
from __future__ import annotations

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileSize
from wtforms import (
    BooleanField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    NumberRange,
    Optional,
    Regexp,
)

from app.forms.validators import StrongPassword
from app.models import (
    ICON_SIZE_DEFAULT,
    ICON_SIZE_MAX,
    ICON_SIZE_MIN,
    MODULE_ACCENTS,
    MODULE_STATUSES,
)


class CreateUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=64)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    full_name = StringField("Full name", validators=[Optional(), Length(max=120)])
    role = SelectField("Role", coerce=int, validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired(), StrongPassword()])
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    is_active = BooleanField("Active", default=True)
    must_change_password = BooleanField("Require password change at next sign in")
    submit = SubmitField("Create user")


class CreateADUserForm(FlaskForm):
    """Add an Active Directory account (no local password is stored).

    Email and full name are optional here: when left blank they are filled
    from the directory lookup performed on submit.
    """

    username = StringField(
        "AD username (sAMAccountName)",
        validators=[DataRequired(), Length(min=1, max=64)],
    )
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    full_name = StringField("Full name", validators=[Optional(), Length(max=120)])
    role = SelectField("Role", coerce=int, validators=[DataRequired()])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Add AD user")


class EditUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=64)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    full_name = StringField("Full name", validators=[Optional(), Length(max=120)])
    role = SelectField("Role", coerce=int, validators=[DataRequired()])
    is_active = BooleanField("Active")
    submit = SubmitField("Save changes")


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), StrongPassword()])
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    must_change_password = BooleanField(
        "Require password change at next sign in", default=True
    )
    submit = SubmitField("Reset password")


class RoleForm(FlaskForm):
    name = StringField("Role name", validators=[DataRequired(), Length(max=50)])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    submit = SubmitField("Save role")


class CategoryForm(FlaskForm):
    """Create/edit a module category (Administrator only)."""

    name = StringField("Category name", validators=[DataRequired(), Length(max=80)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save category")


class ModuleForm(FlaskForm):
    """Create/edit an AI automation module (Administrator only).

    ``icon_builtin`` choices are populated in the route from BUILTIN_ICONS.
    When ``icon_kind`` is "upload" an image file is expected (or, when editing,
    the existing upload is kept if no new file is provided).
    """

    name = StringField("Module name", validators=[DataRequired(), Length(max=120)])
    key = StringField(
        "Key (slug)",
        validators=[
            Optional(), Length(max=64),
            Regexp(r"^[a-z0-9_]+$",
                   message="Use lowercase letters, digits and underscores only."),
        ],
    )
    description = TextAreaField("Description", validators=[Optional(), Length(max=400)])
    # Choices are populated in the route from the Category table; 0 == none.
    category_id = SelectField("Category", coerce=int, validators=[Optional()], choices=[])
    status = SelectField("Status", choices=[(s, s.title()) for s in MODULE_STATUSES])
    accent = SelectField("Accent colour", choices=[(a, a.title()) for a in MODULE_ACCENTS])
    icon_kind = SelectField(
        "Icon source",
        choices=[("builtin", "Built-in icon"), ("upload", "Upload image")],
        default="builtin",
    )
    icon_builtin = SelectField("Built-in icon", validators=[Optional()], choices=[])
    icon_file = FileField(
        "Upload icon",
        validators=[
            Optional(),
            FileAllowed(["png", "jpg", "jpeg", "webp", "svg"],
                        "Images only (png, jpg, webp, svg)."),
            FileSize(max_size=512 * 1024, message="Icon must be 512 KB or smaller."),
        ],
    )
    icon_size = IntegerField(
        "Icon size (px)",
        validators=[DataRequired(), NumberRange(min=ICON_SIZE_MIN, max=ICON_SIZE_MAX)],
        default=ICON_SIZE_DEFAULT,
    )
    is_active = BooleanField("Active", default=True)
    sort_order = IntegerField(
        "Sort order", validators=[Optional(), NumberRange(min=0, max=9999)], default=0
    )
    submit = SubmitField("Save module")
