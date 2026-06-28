"""Generic Row → contract mapping helper.

Most ``*_row_to_contract`` mappers are mechanical: copy each contract field from
the same-named ORM row attribute, with a handful of fields needing a transform
(enum coercion, ``Money.model_validate(...)``, a constant, a derived value).
:func:`map_row` automates the mechanical copy and lets the caller pass the
non-trivial fields as explicit keyword overrides, so the transforms stay visible
while the boilerplate field-by-field copy disappears.

Only use it for genuinely mechanical mappers (every non-overridden contract
field is a direct copy of ``row.<same_name>``). Mappers with conditional
branching or nested queries should stay explicit.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

TContract = TypeVar("TContract", bound=BaseModel)

_MISSING = object()


def map_row(row: object, contract_cls: type[TContract], /, **overrides: object) -> TContract:
    """Build ``contract_cls`` from ``row`` by copying same-named fields.

    For every field declared on ``contract_cls``:

    - if it is given in ``overrides``, use that value (for transforms such as enum
      coercion, ``Money.model_validate(...)``, a constant, or a derived value);
    - else if ``row`` carries a same-named attribute, copy it;
    - else leave it unset so the contract's own default/validation applies.

    The result is built via the contract's normal constructor, so it follows the
    same validation path as an explicit ``contract_cls(field=row.field, ...)`` call.
    Contract-only fields with no matching column (e.g. ``created_by`` / ``version``
    audit fields the rows don't store) fall through to their defaults — matching
    the explicit mappers, which omit them. A *required* field that is neither
    overridden nor present on the row surfaces as the contract's own
    ``ValidationError`` — the signal that the mapper is not purely mechanical and
    that field must be passed explicitly.
    """
    data: dict[str, object] = {}
    for name in contract_cls.model_fields:
        if name in overrides:
            data[name] = overrides[name]
            continue
        value = getattr(row, name, _MISSING)
        if value is not _MISSING:
            data[name] = value
    return contract_cls(**data)
