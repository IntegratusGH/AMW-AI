"""Administrative forms for user management."""
from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    PasswordField,
    SelectField,
    StringField,
    SubmitField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    Optional,
)

from app.forms.validators import StrongPassword


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
