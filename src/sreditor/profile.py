"""A person's own profile: their name and password, shared by the API and the pages."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from sreditor.deps import Services
from sreditor.i18n import N_
from sreditor.users import Password, User


class ProfileError(ValueError):
    """A profile change that is refused; str() is the English msgid."""

    message = N_("This profile change is not allowed")

    def __init__(self) -> None:
        super().__init__(self.message)


class WrongCurrentPassword(ProfileError):
    """The current password given to confirm a change does not match."""

    message = N_("Current password is wrong")


class PasswordChange(BaseModel):
    """The password in use, to confirm, and the new one."""

    model_config = ConfigDict(frozen=True)

    current_password: Annotated[str, Field(max_length=256)]
    password: Password


def rename(app: Services, user: User, display_name: str) -> User:
    """Change how the person's name is shown."""
    return app.users.rename(user.id, display_name)


def change_password(app: Services, user: User, change: PasswordChange) -> None:
    """Set a new password after checking the current one; this signs out every session."""
    current = app.users.password_hash(user.id)
    if current is None or not app.passwords.verify(current, change.current_password):
        raise WrongCurrentPassword
    app.users.set_password(user.id, app.passwords.hash(change.password))
