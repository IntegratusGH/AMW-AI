"""User self-service forms: profile and settings."""
from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional


class ProfileForm(FlaskForm):
    full_name = StringField("Full name", validators=[Optional(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    submit = SubmitField("Save profile")


class SettingsForm(FlaskForm):
    theme = SelectField(
        "Appearance",
        choices=[("light", "Light"), ("dark", "Dark")],
        validators=[DataRequired()],
    )
    submit = SubmitField("Save settings")
