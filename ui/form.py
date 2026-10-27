"""Which form fields changed — the bookkeeping that makes writing to a hand-maintained file safe.

The Setup screen shows the **resolved** configuration: ``clients.yml`` with its ``defaults`` block
merged in. That is the right thing to show — it is what a run will actually use — but it makes
naive saving destructive in a quiet way. Writing every field back would copy each inherited
default into the client's own block, so a form nobody touched would silently freeze `post_type`,
`languages` and `environment` as per-client overrides, and the next change to `defaults` would stop
reaching this client with nothing to show that it had stopped.

So this class remembers what each field was resolved to when the screen loaded, and reports only
the differences. An untouched form produces no edit; a field the operator changed becomes a
per-client override, which is what they meant.

It holds elements only as objects with a ``value`` attribute, so it carries no NiceGUI dependency
and can be tested without one. The widgets, their labels and their wiring stay on the screen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


class HasValue(Protocol):
    """Anything with a ``value`` — every NiceGUI input, and a two-line stub in the tests."""

    value: Any


#: What a field parses to. A list is a YAML sequence (``languages``); everything else is a scalar.
Parsed = str | list[str]


@dataclass(frozen=True)
class Field:
    """One editable field: where it lives in the config, what it was, and how to read it back."""

    path: tuple[str, ...]
    element: HasValue
    initial: Parsed
    parse: Callable[[Any], Parsed]

    @property
    def current(self) -> Parsed:
        return self.parse(self.element.value)

    @property
    def changed(self) -> bool:
        return self.current != self.initial


@dataclass
class FieldSet:
    """Every editable field on a screen, keyed by its path under one client's config block.

    ``prefix`` is prepended to each registered path, and is always ``("clients", client_id)`` in
    practice. It is what keeps a form structurally unable to write into the shared ``defaults``
    block: there is no path a caller can pass that escapes its own client.
    """

    prefix: tuple[str, ...]
    fields: list[Field] = field(default_factory=list)

    def add(
        self,
        path: Sequence[str],
        element: HasValue,
        initial: Parsed,
        parse: Callable[[Any], Parsed] = lambda value: str(value or "").strip(),
    ) -> HasValue:
        """Register a field and return its element, so a caller can register inline."""
        self.fields.append(Field((*self.prefix, *path), element, initial, parse))
        return element

    def changes(self) -> dict[tuple[str, ...], Parsed]:
        """Only the fields whose value differs from what was shown when the screen loaded."""
        return {item.path: item.current for item in self.fields if item.changed}

    def commit(self) -> None:
        """Adopt the current values as the new baseline, once they are on disk.

        Without this the baseline goes stale the moment a save succeeds: the file would say one
        thing and the form's idea of "unchanged" another, so typing a value back to what it was
        before the save would read as *no change* and quietly write nothing. Correcting a
        just-saved mistake is exactly when that must work.
        """
        self.fields = [replace(item, initial=item.current) for item in self.fields]

    def current(self, *path: str) -> Parsed:
        """What one field holds right now, for the cross-field checks a schema cannot express."""
        return self._at(path).current if self._at(path) else ""

    def text(self, *path: str) -> str:
        """:meth:`current` as a scalar, for a field known to be one."""
        value = self.current(*path)
        return value if isinstance(value, str) else ", ".join(value)

    def items(self, *path: str) -> list[str]:
        """:meth:`current` as a list, for a field known to be one."""
        value = self.current(*path)
        return value if isinstance(value, list) else split_list(value)

    def initial(self, *path: str) -> Parsed:
        item = self._at(path)
        return item.initial if item else ""

    def _at(self, path: Sequence[str]) -> Field | None:
        full = (*self.prefix, *path)
        return next((item for item in self.fields if item.path == full), None)

    def __iter__(self) -> Iterator[Field]:
        return iter(self.fields)


def split_list(value: Any) -> list[str]:
    """Parse ``"nl, fr"`` into ``["nl", "fr"]``, dropping blanks a stray comma leaves behind."""
    return [item.strip() for item in str(value or "").split(",") if item.strip()]
