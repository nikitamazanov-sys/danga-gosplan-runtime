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


## Asynchronous portal transport

`POST /v1/jobs` accepts `{job_id, request}` and returns 202 before the model finishes.
`POST /v1/jobs/result` accepts `{job_id}` and returns 202 while running or the unchanged Responses JSON when ready.
Both routes use the same signed timestamp, fresh nonce, and shared secret as `/v1/analyze`.
Job IDs are 32 lowercase hex characters. Repeating the same ID and payload does not repeat the paid call; conflicting payloads are rejected.

Jobs and results are held only in process memory, with two concurrent jobs, at most 16 retained jobs, and 15-minute retention after completion. Restarting the process loses results that the portal has not collected. The portal must collect and save responses promptly; its existing minute scheduler also collects when the browser is closed. A missing job is reported explicitly and never silently resubmitted. Deploy only when no paid jobs are active.

Run transport regression tests with `python3 -m unittest -v test_async_jobs.py`.
