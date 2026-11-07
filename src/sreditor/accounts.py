"""Account flows shared by the JSON API and the web pages: register, verify, sign in, reset, invite.

Each flow sends its email with a link to the page that finishes it (rule web.email.links), and
raises an AccountError subclass that the caller turns into an HTTP status or a form message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sreditor.channels import Channel
from sreditor.i18n import N_, Translator
from sreditor.mail import Mailer
from sreditor.members import AlreadyMember, Invitation, MemberStore, Role
from sreditor.security import Passwords
from sreditor.users import TokenPurpose, User, UserExists, UserStatus, UserStore

VERIFY_TTL = timedelta(days=2)
RESET_TTL = timedelta(hours=1)
INVITE_TTL = timedelta(days=7)


class AccountError(Exception):
    """A flow refused the request. The message is English and safe to show to the user;
    pages translate it, the JSON API returns it as is (i18n.api.english)."""


class InvalidToken(AccountError):
    """The link's token is unknown, already used or expired."""

    def __init__(self) -> None:
        super().__init__(N_("This link is invalid or has expired."))


class WrongCredentials(AccountError):
    """The email and password do not match an account."""

    def __init__(self) -> None:
        super().__init__(N_("Wrong email or password."))


class EmailNotVerified(AccountError):
    """The account exists but its email is not confirmed yet."""

    def __init__(self) -> None:
        super().__init__(N_("Verify your email first: open the link we sent you."))


class AccountDisabled(AccountError):
    """The account was disabled and cannot sign in."""

    def __init__(self) -> None:
        super().__init__(N_("This account is disabled."))


class InvitationNotFound(AccountError):
    """No open invitation has this token."""

    def __init__(self) -> None:
        super().__init__(N_("This invitation is invalid or has expired."))


class InvitationForAnotherEmail(AccountError):
    """The signed-in account is not the one the invitation was sent to."""

    def __init__(self) -> None:
        super().__init__(N_("This invitation is for another email. Sign in with that email."))


class AlreadyAMember(AccountError):
    """The invited person already belongs to the channel."""

    def __init__(self) -> None:
        super().__init__(N_("You are already a member of this channel."))


@dataclass(frozen=True)
class Accounts:
    """Account flows shared by the JSON API and the public pages: sign-up, sign-in, reset, invitations."""

    users: UserStore
    members: MemberStore
    passwords: Passwords
    mailer: Mailer
    public_url: str
    # Emails go out in the language of the request that triggered them (i18n.negotiation).
    translate: Translator = field(default_factory=Translator)

    def link(self, path: str) -> str:
        """An absolute link to `path` on the public site, for emails."""
        return f"{self.public_url.rstrip('/')}{path}"

    def register(self, email: str, display_name: str, password: str) -> None:
        """Create a pending user and email a verification link.

        A taken email gets a notice instead, and the caller answers the same either way,
        so registration cannot reveal which emails have accounts (web.auth.no-enumeration).
        """
        password_hash = self.passwords.hash(password)
        try:
            user = self.users.create(email, display_name, password_hash)
        except UserExists:
            self.mailer.send(
                email,
                self.translate("Someone tried to register with your email"),
                self.translate(
                    "Your email already has an account. If this was you, sign in at %(login)s "
                    "or reset your password at %(reset)s.",
                    login=self.link("/login"),
                    reset=self.link("/forgot-password"),
                ),
            )
            return
        token = self.users.issue_token(user.id, TokenPurpose.VERIFY_EMAIL, VERIFY_TTL)
        self.mailer.send(
            user.email,
            self.translate("Verify your email"),
            self.translate(
                "Confirm your email within two days: %(link)s",
                link=self.link(f"/verify#token={token}"),
            ),
        )

    def verify_email(self, token: str) -> User:
        """Spend a verification token and mark the account's email as confirmed."""
        user_id = self.users.consume_token(token, TokenPurpose.VERIFY_EMAIL)
        if user_id is None:
            raise InvalidToken
        self.users.activate(user_id)
        return self._get(user_id)

    def authenticate(self, email: str, password: str) -> User:
        """The active user with these credentials. Unknown emails take as long as wrong passwords."""
        found = self.users.credentials(email)
        valid = self.passwords.verify(found[1] if found else None, password)
        if found is None or not valid:
            raise WrongCredentials
        user = found[0]
        if user.status is UserStatus.PENDING:
            raise EmailNotVerified
        if user.status is UserStatus.DISABLED:
            raise AccountDisabled
        return user

    def request_password_reset(self, email: str) -> None:
        """Email a reset link if the account exists; the caller answers the same either way."""
        found = self.users.credentials(email)
        if found is None or found[0].status is UserStatus.DISABLED:
            return
        user = found[0]
        token = self.users.issue_token(user.id, TokenPurpose.RESET_PASSWORD, RESET_TTL)
        self.mailer.send(
            user.email,
            self.translate("Reset your password"),
            self.translate(
                "Choose a new password within one hour: %(link)s",
                link=self.link(f"/reset-password#token={token}"),
            ),
        )

    def reset_password(self, token: str, password: str) -> None:
        """Set the new password, sign out every session, and verify the email if it was pending."""
        user_id = self.users.consume_token(token, TokenPurpose.RESET_PASSWORD)
        if user_id is None:
            raise InvalidToken
        self.users.set_password(user_id, self.passwords.hash(password))
        self.users.activate(user_id)

    def invite(self, channel: Channel, email: str, role: Role, inviter: User) -> Invitation:
        """Invite `email` to `channel` with `role`, and email them the link."""
        invitation, token = self.members.invite(channel.slug, email, role, inviter.id, INVITE_TTL)
        self.mailer.send(
            invitation.email,
            self.translate(
                "%(inviter)s invited you to %(channel)s",
                inviter=inviter.display_name,
                channel=channel.name,
            ),
            self.translate(
                "You are invited to %(channel)s as %(role)s. Accept within seven days: %(link)s",
                channel=channel.name,
                role=self.translate.role(role),
                link=self.link(f"/invitations#token={token}"),
            ),
        )
        return invitation

    def open_invitation(self, token: str) -> Invitation:
        """The open invitation for `token`, without accepting it."""
        invitation = self.members.open_invitation(token)
        if invitation is None:
            raise InvitationNotFound
        return invitation

    def accept_invitation(self, token: str, user: User) -> Invitation:
        """Make `user` a member of the invitation's channel and close the invitation."""
        invitation = self.open_invitation(token)
        if invitation.email != user.email.lower():
            raise InvitationForAnotherEmail
        try:
            self.members.accept(invitation, user.id)
        except AlreadyMember as duplicate:
            raise AlreadyAMember from duplicate
        return invitation

    def _get(self, user_id: UUID) -> User:
        user = self.users.get(user_id)
        if user is None:  # pragma: no cover - a token always belongs to an existing user
            raise InvalidToken
        return user
