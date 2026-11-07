"""Form errors read as plain words in the visitor's language, never Pydantic's text (i18n.catalog)."""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import BaseModel, Field, ValidationError

from sreditor.channels import Channel, Color, Platform
from sreditor.i18n import Translator
from sreditor.web.forms import field_errors

PORTUGUESE = Translator("pt-BR")
ENGLISH = Translator("en")


class Numbers(BaseModel):
    """A form with a bounded number and a choice, for the errors they raise."""

    count: Annotated[int, Field(ge=1, le=9)] = 1
    platform: Platform = Platform.TIKTOK


def errors_of(
    model: type[BaseModel], data: dict[str, object], translate: Translator
) -> dict[str, str]:
    try:
        model.model_validate(data)
    except ValidationError as invalid:
        return field_errors(invalid, translate)
    raise AssertionError("the data was valid")


@pytest.mark.parametrize(
    ("model", "data", "field", "expected"),
    [
        (
            Channel,
            {"slug": "Bad Slug!", "name": "x"},
            "slug",
            "Use letras minúsculas e números, com um hífen entre as palavras.",
        ),
        (
            Channel,
            {"slug": "ok", "name": "x", "language": "!"},
            "language",
            "Digite um código de idioma, como en ou pt-BR.",
        ),
        (
            Color,
            {"name": "Pink", "hex": "pink"},
            "hex",
            "Digite a cor como # e seis dígitos hexadecimais, como #ff0066.",
        ),
        (Numbers, {"count": 0}, "count", "Use 1 ou mais."),
        (Numbers, {"count": 10}, "count", "Use 9 ou menos."),
        (Numbers, {"count": "many"}, "count", "Digite um número inteiro."),
        (Numbers, {"platform": "myspace"}, "platform", "Escolha uma das opções."),
    ],
)
def test_field_errors_in_portuguese_explain_the_fix(
    model: type[BaseModel], data: dict[str, object], field: str, expected: str
) -> None:
    assert errors_of(model, data, PORTUGUESE)[field] == expected


def test_field_errors_of_unknown_kind_say_check_this_value() -> None:
    class Flag(BaseModel):
        """A form with a boolean, whose parsing error has no message of ours."""

        is_on: bool

    assert errors_of(Flag, {"is_on": "perhaps"}, ENGLISH) == {"is_on": "Check this value."}
