"""Typer subclass shared by all CLI groups: a file the user has to fix (``ConfigError``) is reported in one line, never a traceback."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import typer

from devorch.config import ConfigError


class DevorchTyper(typer.Typer):
    def command(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        register = super().command(*args, **kwargs)

        def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(func)
            def guarded(*a: Any, **kw: Any) -> Any:
                try:
                    return func(*a, **kw)
                except ConfigError as e:
                    typer.echo(f"오류: {e}", err=True)
                    raise typer.Exit(2) from e

            register(guarded)
            return func  # the module keeps calling the plain function (e.g. in tests)

        return decorate
