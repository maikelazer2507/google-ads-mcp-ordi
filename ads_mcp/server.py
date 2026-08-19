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

"""Entry point for the MCP server."""

from ads_mcp.coordinator import mcp
from ads_mcp import http_routes  # noqa: F401
from ads_mcp import approval_routes  # noqa: F401

# The following imports are necessary to register the resources with the `mcp`
# object, even though they are not directly used in this file.
# Tools are loaded dynamically via reflection in coordinator.py.
# The `# noqa: F401` comment tells the linter to ignore the "unused import"
# warning.
from ads_mcp.resources import (
    discovery,
    metrics,
    release_notes,
    segments,
)  # noqa: F401


import os


def run_server() -> None:
    transport = os.environ.get("GOOGLE_ADS_MCP_TRANSPORT", "").strip()
    if transport == "streamable-http":
        if http_routes._production_configuration_errors():
            raise RuntimeError("Secure HTTP configuration is incomplete.")
        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError as exc:
            raise RuntimeError("Invalid HTTP server configuration.") from exc
        if not 1 <= port <= 65535:
            raise RuntimeError("Invalid HTTP server configuration.")
        mcp.run(
            transport="streamable-http",
            port=port,
            host="0.0.0.0",
            uvicorn_config={"access_log": False},
        )
        return
    if transport == "stdio":
        if (
            os.environ.get("GOOGLE_ADS_MCP_PRODUCTION_MODE", "").strip()
            != "false"
            or os.environ.get("GOOGLE_ADS_MCP_ENVIRONMENT", "").strip()
            != "development"
        ):
            raise RuntimeError(
                "Local stdio requires an explicit development configuration."
            )
        mcp.run(transport="stdio")
        return
    raise RuntimeError(
        "GOOGLE_ADS_MCP_TRANSPORT must be explicitly configured."
    )


if __name__ == "__main__":
    run_server()
