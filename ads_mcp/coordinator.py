# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Module declaring the singleton MCP instance.

The singleton allows other modules to register their tools with the same MCP
server using `@mcp.tool` annotations, thereby 'coordinating' the bootstrapping
of the server.
"""

import os
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider

from ads_mcp.auth_storage import create_client_storage

_REDIRECT_ALLOWLIST_ENV = "GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS"


def parse_allowed_client_redirect_uris(
    value: str | None, *, secure_mode: bool
) -> list[str]:
    """Parse an explicit FastMCP redirect allowlist.

    An absent or empty value deliberately means that no client redirect URI is
    accepted.  In a remotely served environment every entry must be HTTPS and
    name a concrete host.  FastMCP still performs the request-time match; this
    validation prevents a malformed deployment pattern from widening it.
    """
    patterns = [item.strip() for item in (value or "").split(",")]
    patterns = [item for item in patterns if item]
    for pattern in patterns:
        if any(character.isspace() for character in pattern) or "\\" in pattern:
            raise ValueError("Invalid client redirect URI allowlist.")
        try:
            parsed = urlsplit(pattern)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid client redirect URI allowlist.") from exc
        if (
            not parsed.scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65535
        ):
            raise ValueError("Invalid client redirect URI allowlist.")
        if secure_mode and (
            parsed.scheme.casefold() != "https"
            or "*" in pattern
            or parsed.path in {"", "/"}
            or parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Invalid client redirect URI allowlist.")
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise ValueError("Invalid client redirect URI allowlist.")
    return patterns


_CLIENT_ID = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
_CLIENT_SECRET = os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
_BASE_URL = os.environ.get("GOOGLE_ADS_MCP_BASE_URL", "http://localhost:8080")
_JWT_SIGNING_KEY = os.environ.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY")
_SECURE_MODE = (
    os.environ.get("GOOGLE_ADS_MCP_TRANSPORT", "").strip() == "streamable-http"
    or os.environ.get("GOOGLE_ADS_MCP_PRODUCTION_MODE", "").strip().lower()
    == "true"
)

try:
    _ALLOWED_CLIENT_REDIRECT_URIS = parse_allowed_client_redirect_uris(
        os.environ.get(_REDIRECT_ALLOWLIST_ENV), secure_mode=_SECURE_MODE
    )
except ValueError:
    # Empty is FastMCP's fail-closed setting. Readiness reports only a generic
    # configuration failure, without echoing deployment input.
    _ALLOWED_CLIENT_REDIRECT_URIS = []

if _CLIENT_ID and _CLIENT_SECRET:
    client_storage = create_client_storage()
    provider_kwargs: dict[str, Any] = {
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
        "base_url": _BASE_URL,
        "allowed_client_redirect_uris": _ALLOWED_CLIENT_REDIRECT_URIS,
        "required_scopes": [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/adwords",
        ],
    }
    if _JWT_SIGNING_KEY:
        provider_kwargs["jwt_signing_key"] = _JWT_SIGNING_KEY
    if client_storage is not None:
        provider_kwargs["client_storage"] = client_storage

    auth = GoogleProvider(**provider_kwargs)
    mcp = FastMCP("Google Ads Server", auth=auth)
else:
    mcp = FastMCP("Google Ads Server")


def initialize_and_mount_tools(parent_mcp: FastMCP) -> None:
    """Loads the tools configuration and dynamically mounts the tools sub-servers."""
    from ads_mcp.config import ToolsConfig
    import importlib
    import pkgutil
    import ads_mcp.tools as tools_pkg

    # Map of category name -> FastMCP sub-server
    sub_servers = {}

    # Discover and dynamically load all tool modules
    for _, module_name, _ in pkgutil.iter_modules(tools_pkg.__path__):
        full_module_name = f"ads_mcp.tools.{module_name}"
        module = importlib.import_module(full_module_name)

        # Find any FastMCP instances defined in the module
        for attr_name in dir(module):
            attr_val = getattr(module, attr_name)
            if isinstance(attr_val, FastMCP):
                category = attr_val.name
                sub_servers[category] = attr_val

    config = ToolsConfig.load()

    for category, sub_mcp in sub_servers.items():
        if not config.is_namespace_enabled(category):
            continue

        # Filter disabled tools inside the sub-server before mounting
        tool_names = []
        for key, val in sub_mcp.local_provider._components.items():
            if key.startswith("tool:"):
                tool_names.append(val.name)

        for name in tool_names:
            if not config.is_tool_enabled(category, name):
                sub_mcp.local_provider.remove_tool(name)

        # Determine prefix/namespace
        namespace_prefix = config.get_namespace_prefix(category)

        # Mount the sub-server
        parent_mcp.mount(sub_mcp, namespace=namespace_prefix or None)


# Automatically initialize and mount tools upon import
initialize_and_mount_tools(mcp)
