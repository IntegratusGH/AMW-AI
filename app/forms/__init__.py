"""Forms package."""
from app.forms.admin import (
    CategoryForm,
    CreateADUserForm,
    CreateUserForm,
    EditUserForm,
    ModuleForm,
    ResetPasswordForm,
    RoleForm,
)
from app.forms.auth import ChangePasswordForm, LoginForm
from app.forms.profile import ProfileForm, SettingsForm
from app.forms.registration import CRBookUploadForm, ReceiptUploadForm

__all__ = [
    "LoginForm",
    "ChangePasswordForm",
    "CreateUserForm",
    "CreateADUserForm",
    "EditUserForm",
    "ResetPasswordForm",
    "RoleForm",
    "CategoryForm",
    "ModuleForm",
    "ProfileForm",
    "SettingsForm",
    "CRBookUploadForm",
    "ReceiptUploadForm",
]
