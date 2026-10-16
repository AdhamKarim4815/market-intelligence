"""The layering rule, enforced rather than documented.

One source of business truth is the central architectural requirement of
this project: the CLI, the web pages and the future iOS client must call the
same calculation, or they will eventually disagree about a portfolio value
and there will be no way to tell which one is right.

Documentation does not enforce that. A test walking the import graph does.
The rules below are deliberately few and absolute, because a layering rule
with exceptions is a layering rule that stops being checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "akmarkets"

# subpackage -> subpackages it may not import from.
FORBIDDEN: dict[str, set[str]] = {
    # Pure calculation. No storage, no network, no framework. This is what
    # makes the financial engine testable to the cent without a database.
    "domain": {"db", "providers", "services", "web", "api", "cli", "jobs", "ai", "export"},
    # Adapters translate an external response into a domain record. They do
    # not decide anything and do not read the database.
    "providers": {"db", "services", "web", "api", "cli", "jobs", "export"},
    # Presentation. A route reads parameters, calls one service, renders.
    # Reaching past services into db or providers is how web-only logic
    # starts.
    "web": {"providers", "cli", "jobs", "export"},
    "api": {"providers", "cli", "jobs", "export", "web"},
    "cli": {"providers", "web"},
    # Persistence knows about the domain (it stores those values) but
    # nothing above it.
    "db": {"providers", "services", "web", "api", "cli", "jobs", "ai", "export"},
}

ROOT_MODULES = {"config", "secrets", "activity"}


def _subpackage(path: Path) -> str:
    relative = path.relative_to(SRC)
    return relative.parts[0] if len(relative.parts) > 1 else ""


def _imported_subpackages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.ImportFrom):
            # `from . import x` inside the package: level > 0, no module.
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("akmarkets."):
                    found.add(alias.name.split(".")[1])
            continue
        if module and module.startswith("akmarkets."):
            part = module.split(".")[1]
            if part not in ROOT_MODULES:
                found.add(part)
    return found


def _source_files() -> list[Path]:
    return sorted(path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts)


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_module_respects_layering(path: Path) -> None:
    layer = _subpackage(path)
    forbidden = FORBIDDEN.get(layer)
    if not forbidden:
        return
    violations = sorted(_imported_subpackages(path) & forbidden)
    assert not violations, (
        f"{path.relative_to(SRC)} is in the '{layer}' layer and imports "
        f"{violations}, which that layer may not depend on. Route the call "
        f"through akmarkets.services instead."
    )


def test_domain_imports_no_framework() -> None:
    """The financial engine must not depend on SQLAlchemy, FastAPI or httpx.

    Not a style preference. If a calculation needs a session or a request to
    run, it cannot be tested against a hand-computed reference, and every
    figure in this product ultimately has to be defensible against one.
    """
    banned = {"sqlalchemy", "fastapi", "httpx", "starlette", "typer", "keyring"}
    offenders: list[str] = []
    for path in (SRC / "domain").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in banned:
                    offenders.append(f"{path.relative_to(SRC)} imports {name}")
    assert not offenders, offenders
