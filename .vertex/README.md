# Google Agent Platform LiteLLM proxy

Google Agent Platform (`aiplatform.googleapis.com`) uses LiteLLM's built-in `vertex_ai` provider with automatically refreshed Google Application Default Credentials (ADC).

The OpenAI-compatible base URL is `http://127.0.0.1:14604/v1`. Models are `vertex-default` (Gemini 3.8 Flash), `gemini-3.8-flash`, `gemini-3.5-flash`, and `gemini-3.5-flash-lite`. Model availability depends on the selected Google Cloud project.

The router retries transient upstream failures up to three times after the initial attempt (four attempts total), with backoff starting at two seconds and provider `Retry-After` support. Rate limits, timeouts, connection errors, and server failures are retryable. Invalid requests, authentication failures, and content-policy failures return immediately. Exhausted retries propagate the upstream error through the proxy's OpenAI-compatible error response to the client. Both LiteLLM and router retry settings are three so the global setting does not suppress server-error retries. Deployment cooldowns are disabled because each model has only one upstream deployment.

RisuAI must keep the connection open for retries to finish; each upstream attempt retains the existing 180-second timeout. Retries apply before a response is committed. A stream that has already delivered content cannot transparently restart; failures during that stream are returned through the existing stream error handling. Client JSON parsing errors and the local concurrency limit are outside upstream retry handling.

Copy `vertex.local.env.example` to `vertex.local.env`, set the project ID and a private proxy key, and authenticate with `gcloud auth application-default login` if ADC is absent. Alternatively, save a service account JSON in the ignored `service-account.local.json` and set `GOOGLE_APPLICATION_CREDENTIALS` to its absolute path in `vertex.local.env`. The local environment file takes precedence over inherited variables for this server process. The project must have billing and `aiplatform.googleapis.com` enabled, and the credential must have `aiplatform.endpoints.predict` permission. CLI login alone does not establish ADC. Google access tokens are refreshed automatically and are not stored in the configuration.

Run `powershell -NoProfile -ExecutionPolicy Bypass -File .vertex/start-server.ps1` from the repository root. Run `.vertex/install-startup.ps1` to register the hidden server for Windows sign-in. Duplicate launches on the same port exit without starting another instance. The server uses the existing `.venv` and requires the repository to remain at its installed location.

Read `VERTEX_PROXY_KEY` from `vertex.local.env` for the client's API key. Logs are in `.vertex/logs/server.log` and `.vertex/logs/server.err.log`; the Python process ID is saved in `.vertex/server.pid`. Stop that process to stop the server. Remove `litellm.exe vertex.lnk` from the user Startup folder to disable automatic startup.

To verify from PowerShell, use:

```powershell
$VertexKey = ((Get-Content .vertex/vertex.local.env | Where-Object { $_ -like 'VERTEX_PROXY_KEY=*' }) -split '=', 2)[1]
$VertexHeaders = @{ Authorization = "Bearer $VertexKey" }
Invoke-RestMethod http://127.0.0.1:14604/v1/models -Headers $VertexHeaders
$VertexBody = @{ model = 'vertex-default'; messages = @(@{ role = 'user'; content = 'Reply only OK.' }); max_tokens = 64 } | ConvertTo-Json -Depth 5
Invoke-RestMethod http://127.0.0.1:14604/v1/chat/completions -Method Post -Headers $VertexHeaders -ContentType 'application/json' -Body $VertexBody
```

If generation returns `BILLING_DISABLED`, enable billing for the configured project and retry the same request. An operational proxy and model listing do not establish that upstream inference is available.
