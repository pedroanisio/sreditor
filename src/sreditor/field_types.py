"""Constrained string types shared by the stored models, so one value has one rule everywhere.

A channel slug, a language tag or a SHA-256 is checked the same way in the channel that defines
it and in every episode, take, file or link that refers to it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

SLUG_LENGTH = 64
TITLE_LENGTH = 200
TOKEN_LENGTH = 256
SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
LANGUAGE_PATTERN = r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$"  # a BCP 47 tag such as pt-BR
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MEDIA_TYPE_PATTERN = r"^[a-z]+/[a-z0-9.+-]+$"
VOICE_ID_LENGTH = 64
VOICE_ID_PATTERN = rf"^[A-Za-z0-9]{{1,{VOICE_ID_LENGTH}}}$"
# A script may leave its voice empty and take the brand's narrator voice instead.
VOICE_ID_OR_EMPTY_PATTERN = rf"^[A-Za-z0-9]{{0,{VOICE_ID_LENGTH}}}$"

ChannelSlug = Annotated[
    str, StringConstraints(pattern=SLUG_PATTERN, min_length=1, max_length=SLUG_LENGTH)
]
LanguageTag = Annotated[str, StringConstraints(pattern=LANGUAGE_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
MediaType = Annotated[str, StringConstraints(pattern=MEDIA_TYPE_PATTERN)]
VoiceId = Annotated[str, StringConstraints(pattern=VOICE_ID_PATTERN)]
VoiceIdOrEmpty = Annotated[str, StringConstraints(pattern=VOICE_ID_OR_EMPTY_PATTERN)]
Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_LENGTH)
]
EmailToken = Annotated[str, StringConstraints(min_length=1, max_length=TOKEN_LENGTH)]
