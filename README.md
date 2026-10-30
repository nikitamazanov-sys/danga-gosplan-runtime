# Authenticated drawing processor

Minimal Python WSGI runtime for signed requests to the official OpenAI Responses API.

No API keys, shared secrets, drawings, customer data, or portal source files belong in this repository.

## Configuration

Set these only in the hosting service's private environment settings:

- `OPENAI_API_KEY`
- `WORKER_SHARED_SECRET` (at least 48 random characters)
- `OPENAI_MODEL` (a currently supported model identifier)

`GET /healthz` checks the running process. `GET /readyz` validates configuration without calling OpenAI. `POST /v1/analyze` requires an HMAC signature and fresh nonce.

The container runs a single non-root Gunicorn process. Files are processed in memory and are not persisted by this application. Rate and replay protection are process-local; use one instance. Restarting clears these limits. Usage counts are logged without document contents.

Hosting location does not change OpenAI account or regional eligibility. Verify provider requirements before use.
