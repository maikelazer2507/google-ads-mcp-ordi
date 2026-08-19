# Google Ads MCP Server

This repo contains the source code for running an
[MCP](https://modelcontextprotocol.io) server that interacts with the
[Google Ads API](https://developers.google.com/google-ads/api).

## Tools

The server uses the
[Google Ads API](https://developers.google.com/google-ads/api/reference/rpc/latest/overview)
to provide several
[Tools](https://modelcontextprotocol.io/docs/concepts/tools) and [Resources](https://modelcontextprotocol.io/docs/concepts/tools) for use with LLMs and AI agents.

### Tools available

- `search`: Retrieves information about the Google Ads account.
- `get_resource_metadata`: Retrieves metadata about a Google Ads API resource type, for example "campaign". This is useful to understand the structure of the data and what fields are available for querying.
- `list_accessible_customers`: Returns ids of customers directly accessible
  by the user authenticating the call and inside the deployment read allowlist.
- Typed read-only diagnostics for configuration/conversions, campaign
  performance, device/time/network/geography/keyword breakdowns, budget pacing,
  search-term candidates, change history, policy/recommendations, and anomaly
  triage.
- Guarded write tools under the `changes` namespace:
  `preview_*` and `apply_*` pairs for pausing campaigns and ad groups, ad
  status, campaign budgets, keyword status, adding or removing ad-group
  negative keywords, and ad final URLs. Campaign/ad-group reactivation remains
  manual until its full delivery context is validated on a test account.
- Read-only change-set audit tools for execution status, ordered approval and
  execution events, and explicit reconciliation guidance for uncertain writes.

### Guarded write workflow

Write tools are disabled until explicit account, Firestore, environment, and
human-approval controls are configured. Every supported change follows this
workflow:

1. A `preview_*` tool reads the live object, validates the proposed mutation
   with the Google Ads API using `validate_only`, persists a short-lived
   `PENDING` change set, and returns an opaque token plus an HTTPS approval URL.
   It does not change account state.
2. The owner opens the separate approval URL, signs in with a server-allowlisted
   Google identity, reviews the exact persisted payload, and performs a second
   confirmation. Approval is not an MCP tool and cannot be expressed as chat
   text or a tool argument.
3. The matching `apply_*` tool atomically consumes that exact approval once,
   re-reads live state, aborts on detected drift, validates again, applies only
   the approved field, immediately verifies the result, and records a redacted
   after-state hash. Google Ads cannot make the re-read and mutation one atomic
   API operation, so short approval TTLs and the post-read remain required.

The initial write surface deliberately excludes deletes, account-access
changes, bidding-strategy changes, conversion configuration, customer-data
uploads, and bulk mutations. Shared budgets are blocked, budget increases are
capped at 10 percent, and landing-page hosts must be allowlisted.
Final-URL changes are limited to responsive search ads with exactly one
non-removed ad-group use; shared global ad IDs fail closed.

Configure these environment variables before enabling write use:

- `GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS`: Comma-separated Google Ads customer
  IDs permitted for writes. Hyphens are accepted.
- `GOOGLE_ADS_MCP_READ_CUSTOMER_IDS`: Comma-separated customer IDs visible to
  read tools. Production reads fail closed when this is missing.
- `GOOGLE_ADS_MCP_OPERATOR_EMAILS`: Google emails permitted to use read,
  diagnostics, preview, and apply tools. Keep Maria and Maikel separate.
- `GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE`: Must be `firestore` in production.
- `GOOGLE_ADS_MCP_ENVIRONMENT`: Stable lowercase deployment name such as
  `production`.
- `GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS`: Optional approval lifetime in
  seconds. Defaults to 900 and is capped at 3600.
- `GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS`: Comma-separated exact hostnames
  permitted for final-URL changes.
- `GOOGLE_ADS_MCP_APPROVAL_BASE_URL`: Public HTTPS origin used to build the
  separate approval page.
- `GOOGLE_ADS_MCP_APPROVER_EMAILS`: Server-side approver email allowlist.
- `GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID`: Google Identity Services web
  client ID, supplied through Secret Manager.
- `GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY`: Independent 32+ character secret for
  five-minute review sessions, supplied through Secret Manager.
- `GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY`: Separate 32+ character secret that
  seals an approved payload against direct datastore tampering.

An MCP client's permission prompt is an additional control, not a replacement
for the server-side preview and approval boundary.

### Configuring and Namespacing Tools

The Google Ads MCP server uses the `tools_config.yaml` to let you selectively enable or disable individual tools or tool categories (namespaces) and customize their namespace prefixes.

A default `tools_config.yaml` with all tools enabled is bundled with the package, so the server works out of the box with no extra setup. To customize your installation, the server resolves the configuration in the following order:

1. An explicit path set via the `GOOGLE_ADS_MCP_TOOLS_CONFIG` environment variable.
2. A `tools_config.yaml` file in the current working directory.
3. The default `tools_config.yaml` bundled with the package.

If an explicitly requested configuration file (via the environment variable) is missing, or any resolved file is invalid, the server raises an error and fails to start.

#### Configuration Example:
```yaml
namespaces:
  # Option 1: Enable category 'customers' with default prefix -> "customers_list_accessible_customers"
  customers: true

  # Option 2: Enable category 'search' with a custom prefix -> "query_search"
  search: "query"

  # Option 3: Fine-grained control over tools in a category
  metadata:
    enabled: true
    prefix: "metadata"
    enabled_tools:
      - get_resource_metadata: true
```


### Resources available

- `discovery-document`: Retrieve the Google Ads API discovery document. Provides the discovery document for the latest version of the Google Ads API, which describes the API surface, including resources, methods, and schemas. Host LLMs should access this resource to understand the structure of the Google Ads API and discover available features.
- `metrics`: Retrieve information about the metrics available for reporting in the Google Ads API.
- `segments`: Retrieve information about the segments available for reporting in the Google Ads API.
- `release-notes`: Retrieve the release notes for the latest version of the Google Ads API.

## Notes

1.  The MCP Server will expose your data to the Agent or LLM that you connect to it.
1.  If you have technical issues, please use the [GitHub issue tracker](https://github.com/googleads/google-ads-mcp/issues).
1.  To help us collect usage data, you will notice an extra header has been added to your API calls: this data is used to improve the product.

## Setup instructions

Setup involves the following steps:

1.  Configure Python.
1.  Configure Developer Token.
1.  Enable APIs in your project
1.  Configure Credentials.
1.  Configure your MCP client.

### Configure Python

[Install pipx](https://pipx.pypa.io/stable/#install-pipx).

### Configure Developer Token

Follow the instructions for [Obtaining a Developer Token](https://developers.google.com/google-ads/api/docs/get-started/dev-token).

Your developer token must have at least [Explorer access](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) to query production accounts. New tokens may be automatically upgraded to Explorer access; if not, you can apply through the API Center. See the [access levels documentation](https://developers.google.com/google-ads/api/docs/get-started/dev-token#access-levels) for details.

If you see the error *"The developer token is only approved for use with test
accounts"*, your token does not yet have access to production accounts. See the
[access levels documentation](https://developers.google.com/google-ads/api/docs/access-levels)
for how to request the access level you need.

### Enable APIs in your project

[Follow the instructions](https://support.google.com/googleapi/answer/6158841)
to enable the following APIs in your Google Cloud project:

* [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com)

### Configure Credentials
#### Option 1: Using FastMCP OAuth Proxy

The server supports FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication. This is useful when running the server as a web service.

Remote HTTP mode is fail-closed. Set the following environment variables:

- `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`: Your Google Cloud OAuth 2.0 Client ID.
- `GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET`: Your Google Cloud OAuth 2.0 Client Secret.
- `GOOGLE_ADS_MCP_TRANSPORT`: Must be `streamable-http` remotely.
- `GOOGLE_ADS_MCP_PRODUCTION_MODE`: Must be exactly `true` remotely.
- `GOOGLE_ADS_MCP_BASE_URL`: Required public HTTPS origin of the server.
- `GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS`: Comma-separated exact HTTPS
  MCP-client callback URLs. Wildcards and origin-only entries are rejected.
- `GOOGLE_ADS_MCP_JWT_SIGNING_KEY`: Required 32+ character secret used to sign
  FastMCP sessions across instances.
- `GOOGLE_ADS_MCP_STORAGE_TYPE`: Must be `firestore` in production.
- `GOOGLE_ADS_MCP_STORAGE_PATH`: (Optional) Directory path for `filetree` persistent storage.
- `GOOGLE_ADS_MCP_STORAGE_REDIS_URL`: (Optional) Redis URL for `redis` persistent storage.
- `GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT`: (Optional) Google Cloud project for `firestore` persistent storage. Defaults to the project inferred from Application Default Credentials. Setting it selects the `firestore` backend even if `GOOGLE_ADS_MCP_STORAGE_TYPE` is unset.
- `GOOGLE_ADS_MCP_STORAGE_FIRESTORE_DATABASE`: (Optional) Firestore database name for `firestore` persistent storage. Defaults to `(default)`.
- `GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY`: Required 32+ character key for stored OAuth tokens.
- `GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION`: Must be `false` in remote mode.
- `GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS`: Required to enable guarded write tools.
- `GOOGLE_ADS_MCP_READ_CUSTOMER_IDS`: Required for fail-closed production reads.
- `GOOGLE_ADS_MCP_OPERATOR_EMAILS`: Required production operator allowlist.
- `GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE`: Durable change-set backend; use
  `firestore` in production.
- `GOOGLE_ADS_MCP_ENVIRONMENT`: Stable change-set environment binding.
- `GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS`: Optional change-set lifetime.
- `GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS`: Required for ad final-URL changes.
- `GOOGLE_ADS_MCP_APPROVAL_BASE_URL`: HTTPS origin for the non-MCP approval UI.
- `GOOGLE_ADS_MCP_APPROVER_EMAILS`: Authorized human approvers.
- `GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID`: Google OIDC audience/client ID.
- `GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY`: Secret for short review sessions.
- `GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY`: Independent secret that seals
  approved change sets.

The `redis` and `firestore` backends need their storage library installed
alongside the server: `pip install py-key-value-aio[redis]` and
`pip install google-ads-mcp[firestore]` respectively.

Once this is enabled, you can authenticate to the API through your MCP client.

The server never infers its transport from the presence of credentials. Remote
mode requires `GOOGLE_ADS_MCP_TRANSPORT=streamable-http`; local stdio requires
the explicit development triplet documented in `docs/SECURITY_MODEL.md`.

You will need to run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8080/mcp`).

#### Option 2: Configure credentials using Application Default Credentials

Configure your [Application Default Credentials
(ADC)](https://cloud.google.com/docs/authentication/provide-credentials-adc).
Make sure the credentials are for a user with access to your Google Ads
accounts or properties.

Credentials must include the Google Ads API scope:

```
https://www.googleapis.com/auth/adwords
```

Check out
[Manage OAuth Clients](https://support.google.com/cloud/answer/15549257)
for how to create an OAuth client.

Here are some sample `gcloud` commands you might find useful:


- Set up ADC using user credentials and an OAuth desktop or web client after
  downloading the client JSON to `YOUR_CLIENT_JSON_FILE`.

  ```shell
  gcloud auth application-default login \
    --scopes https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform \
    --client-id-file=YOUR_CLIENT_JSON_FILE
  ```

- Set up ADC using service account impersonation.

  ```shell
  gcloud auth application-default login \
    --impersonate-service-account=SERVICE_ACCOUNT_EMAIL \
    --scopes=https://www.googleapis.com/auth/adwords,https://www.googleapis.com/auth/cloud-platform
  ```

When the `gcloud auth application-default` command completes, copy the
`PATH_TO_CREDENTIALS_JSON` file location printed to the console in the
following message. You will need this for a later step!

```
Credentials saved to file: [PATH_TO_CREDENTIALS_JSON]
```

#### Option 3: Configure credentials using the Google Ads API Python client library.

[Follow the instructions](https://developers.google.com/google-ads/api/docs/client-libs/python/)
to setup and configure the Google Ads API Python client library

If you have already done this and have a working `google-ads.yaml` , you can reuse this file!

In the utils.py file, change get_googleads_client() to use the load_from_storage() method.

### Configure your MCP client

Add the server to your MCP client's configuration. Below are examples for
popular clients.

#### Antigravity CLI / Antigravity Code Assist

1.  Install [Antigravity CLI](https://antigravity.google/product/antigravity-cli) or Antigravity Code Assist.

1.  Configure your server. Refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp) for details on setting up MCP servers.

- Option 1: Using FastMCP OAuth Proxy (Streamable HTTP)

  You can run the server as a separate process and configure your MCP client to connect to the SSE endpoint (e.g., `http://localhost:8080/mcp`).
  This also allows using FastMCP's [OAuth proxy](https://gofastmcp.com/servers/auth/oauth-proxy) feature for dynamic user authentication.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "httpUrl":"http://localhost:8080/mcp",
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"                        
          }
        }
      }
    }
    ```

- Option 2: the Application Default Credentials method

    Replace `PATH_TO_CREDENTIALS_JSON` with the path you copied in the previous
    step.

    We also recommend that you add a `GOOGLE_CLOUD_PROJECT` attribute to the
    `env` object. Replace `YOUR_PROJECT_ID` in the following example with the
    [project ID](https://support.google.com/googleapi/answer/7014113) of your
    Google Cloud project.

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

- Option 3: the Python client library method

    ```json
    {
      "mcpServers": {
        "google-ads-mcp": {
          "command": "pipx",
          "args": [
            "run",
            "--spec",
            "git+https://github.com/googleads/google-ads-mcp.git",
            "google-ads-mcp"
          ],
          "env": {
            "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN"
          }
        }
      }
    }
    ```

#### Login Customer Id

If your access to the customer account is through a manager account, you will
need to add the customer ID of the manager account to the settings file.

See [here](https://developers.google.com/google-ads/api/docs/concepts/call-structure#cid) for details.

The final file will look like this:

  ```json
  {
    "mcpServers": {
      "google-ads-mcp": {
        "command": "pipx",
        "args": [
          "run",
          "--spec",
          "git+https://github.com/googleads/google-ads-mcp.git",
          "google-ads-mcp"
        ],
        "env": {
          "GOOGLE_APPLICATION_CREDENTIALS": "PATH_TO_CREDENTIALS_JSON",
          "GOOGLE_PROJECT_ID": "YOUR_PROJECT_ID",
          "GOOGLE_ADS_DEVELOPER_TOKEN": "YOUR_DEVELOPER_TOKEN",
          "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "YOUR_MANAGER_CUSTOMER_ID"
        }
      }
    }
  }
  ```

#### Other MCP clients (Claude Code, Cursor, VS Code, etc.)

The `mcpServers` block format is the same across all MCP clients. Add the configuration shown above to the appropriate settings file for your client (e.g., `~/.claude/settings.json` for Claude Code, `.cursor/mcp.json` for Cursor, `.vscode/mcp.json` for VS Code with Copilot).

## Deployment to Google Cloud Platform

Instead of hosting this MCP server locally, you can host it on Google Cloud Run or on any other cloud-based infrastructure. This is useful if you want to share the server across different agents or run it as a web service.

Note that this only supports authentication with an OAuth Client ID and Client Secret pair through the OAuth proxy (Option #1 above).

### Prerequisites

1.  A Google Cloud project.
2.  The `gcloud` CLI installed, authenticated, and active project set.
    ```shell
    gcloud config set project YOUR_PROJECT_ID
    ```

### Steps 1 and 2: Build, canary, promote, or roll back

Do not put developer tokens, OAuth secrets, signing keys, encryption keys, or
approval keys in command-line environment values. Use the plan-first scripts in
[`deploy/README.md`](deploy/README.md). They require immutable image digests,
numeric Secret Manager versions, a least-privilege runtime service account,
Firestore, health/readiness probes, a zero-traffic canary, explicit promotion,
and reversible Cloud Run traffic.

The full release gate, separate Maria/Maikel test, monitoring, incident, backup,
and rollback procedures are in
[`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md). The guarded write
revision must not receive production traffic until those gates pass.

### Step 3: Configure MCP Client

Once deployed, update your MCP client configuration (refer to the docs at [https://antigravity.google/docs/mcp](https://antigravity.google/docs/mcp)) to use the Cloud Run URL.

```json
{
  "mcpServers": {
    "google-ads-mcp": {
      "httpUrl": "https://your-cloud-run-url.a.run.app/mcp"
    }
  }
}
```

## Try it out

Launch your MCP client. You should see `google-ads-mcp` listed in the
available servers.

Here are some sample prompts to get you started:

- Ask what the server can do:

  ```
  what can the ads-mcp server do?
  ```

- Ask about customers:

  ```
  what customers do I have access to?
  ```

- Ask about campaigns 

  ```
  How many active campaigns do I have?
  ```

  ```
  How is my campaign performance this week?
  ```

### Note about Customer ID

Your agent will need and ask for a customer id for most prompts. If you are 
moving between multiple customers, including the customer ID in the prompt may
be simpler.

```
How many active campaigns do I have for customer id 1234567890
```

## Contributing

Contributions welcome! See the [Contributing Guide](CONTRIBUTING.md).
