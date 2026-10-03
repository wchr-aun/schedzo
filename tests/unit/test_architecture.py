"""Keep HTTP adapters behind the application-service boundary."""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "router_path",
    sorted((Path(__file__).resolve().parents[2] / "app" / "routers").glob("*.py")),
    ids=lambda path: path.name,
)
def test_routers_do_not_expose_provider_clients(router_path):
    forbidden_modules = {"app.services.monzo", "app.runtime"}
    forbidden_names = {
        "MonzoClient",
        "ApplicationResources",
        "get_monzo_client",
        "_get_monzo_client",
        "get_resources",
        "monzo_client",
        "monzo_client_scope",
        "AsyncClient",
        "Client",
    }
    for node in ast.walk(ast.parse(router_path.read_text())):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden_modules
            assert not forbidden_names.intersection(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            assert not forbidden_modules.intersection(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden_names
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_names
