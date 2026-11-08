"""Authenticated, bounded OpenAI Responses worker. No drawings are persisted."""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

MAX_BODY = 44 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
WINDOW = 120
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_translation_slot = threading.BoundedSemaphore(1)
_nonces = {}
_calls = []
_poll_calls = []
_jobs = {}
JOB_TTL = 900
log = logging.getLogger("gosplan")


class Failure(Exception):
    def __init__(self, status, code, parameter=None):
        self.status, self.code, self.parameter = status, code, parameter


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def settings():
    key = os.environ.get("OPENAI_API_KEY", "")
    secret = os.environ.get("WORKER_SHARED_SECRET", "")
    model = os.environ.get("OPENAI_MODEL", "")
    if not key.startswith("sk-") or len(secret) < 48 or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", model):
        raise Failure(503, "worker_not_configured")
    return key, secret, model


def authenticate(environ, body, secret, poll=False):
    stamp = environ.get("HTTP_X_GOSPLAN_TIMESTAMP", "")
    nonce = environ.get("HTTP_X_GOSPLAN_NONCE", "")
    signature = environ.get("HTTP_X_GOSPLAN_SIGNATURE", "")
    if not re.fullmatch(r"[0-9]{10}", stamp) or not re.fullmatch(r"[a-f0-9]{32,64}", nonce):
        raise Failure(401, "invalid_signature")
    now = time.time()
    if abs(now - int(stamp)) > WINDOW:
        raise Failure(401, "expired_signature")
    expected = hmac.new(secret.encode(), stamp.encode() + b"\n" + nonce.encode() + b"\n" + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise Failure(401, "invalid_signature")
    with _lock:
        for old in [n for n, expiry in _nonces.items() if expiry <= now]:
            del _nonces[old]
        calls = _poll_calls if poll else _calls
        calls[:] = [t for t in calls if now - t < 60]
        if nonce in _nonces:
            raise Failure(409, "replayed_request")
        if len(_nonces) >= 4096 or len(calls) >= (120 if poll else 10):
            raise Failure(429, "worker_rate_limit")
        _nonces[nonce] = now + 2 * WINDOW
        calls.append(now)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate field")
        obj[key] = value
    return obj



def usage_metrics(response_bytes, model):
    """Extract token counts and a *non-billing* estimate, without document content."""
    try:
        response = json.loads(response_bytes)
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        detail = usage.get("input_tokens_details") or {}
        cached = detail.get("cached_tokens", 0) if isinstance(detail, dict) else None
        numbers = (input_tokens, output_tokens, cached)
        if any(type(value) is not int or value < 0 or value > 100_000_000 for value in numbers):
            return None
        if cached > input_tokens:
            return None
        # Standard short-context public list price, not an invoice or a spending cap.
        rates = {"gpt-6.1-terra": (1.00, 0.05, 5.00)}
        estimate = None
        if model in rates:
            ordinary_in, cached_in, out = rates[model]
            estimate = ((input_tokens - cached) * ordinary_in
                        + cached * cached_in + output_tokens * out) / 1_000_000
        return {"input": input_tokens, "output": output_tokens,
                "cached": cached, "estimated_usd": estimate}
    except (TypeError, ValueError, AttributeError):
        return None


def payload_from(body, model):
    try:
        raw = json.loads(body, object_pairs_hook=unique_object)
        if not isinstance(raw, dict) or set(raw) - {"model", "store", "instructions", "input", "max_output_tokens", "text"}:
            raise ValueError()
        instructions = raw.get("instructions")
        if not isinstance(instructions, str) or not 1 <= len(instructions) <= 24000:
            raise ValueError()
        messages = raw.get("input")
        if not isinstance(messages, list) or len(messages) != 1 or set(messages[0]) != {"role", "content"} or messages[0]["role"] != "user":
            raise ValueError()
        content = messages[0]["content"]
        if not isinstance(content, list) or not 1 <= len(content) <= 16:
            raise ValueError()
        for item in content:
            kind = item.get("type")
            if kind == "input_text":
                if set(item) != {"type", "text"} or not isinstance(item["text"], str) or len(item["text"]) > 64000:
                    raise ValueError()
            elif kind in {"input_image", "input_file"}:
                if kind == "input_image":
                    if set(item) - {"type", "image_url", "detail"} or item.get("detail", "high") not in {"high", "low", "auto"}:
                        raise ValueError()
                    data, allowed = item["image_url"], {"image/png", "image/jpeg", "image/webp"}
                else:
                    if set(item) != {"type", "filename", "file_data"} or not re.fullmatch(r"[^/\\\x00-\x1f]{1,160}\.pdf", item["filename"], re.I):
                        raise ValueError()
                    data, allowed = item["file_data"], {"application/pdf"}
                header, encoded = data.split(",", 1)
                if header not in {"data:" + mime + ";base64" for mime in allowed}:
                    raise ValueError()
                decoded = base64.b64decode(encoded, validate=True)
                if not decoded or len(decoded) > 30 * 1024 * 1024:
                    raise ValueError()
            else:
                raise ValueError()
        # JSON mode validates the input context separately from instructions.
        normalized = [{"role": "user", "content": [{"type": "input_text", "text": "Return the result as JSON."}, *content]}]
        return {"model": model, "store": False, "instructions": instructions, "input": normalized,
                "max_output_tokens": 12000, "text": {"format": {"type": "json_object"}}}
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise Failure(400, "invalid_payload") from None


def request_openai(payload, key):
    request = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=240) as response:
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise Failure(502, "upstream_response_too_large")
            result = json.loads(body)
            if not isinstance(result, dict) or result.get("status") != "completed":
                raise Failure(502, "upstream_incomplete")
            metrics = usage_metrics(body, payload["model"])
            if metrics is not None:
                # These records survive Free-tier sleeps in the hosting logs.
                log.warning(
                    "openai_usage model=%s input=%d cached=%d output=%d estimated_usd=%s",
                    payload["model"], metrics["input"], metrics["cached"], metrics["output"],
                    "%.6f" % metrics["estimated_usd"] if metrics["estimated_usd"] is not None else "unknown"
                )
            return body
    except urllib.error.HTTPError as error:
        code = "upstream_error"
        parameter = None
        try:
            parsed = json.loads(error.read(32768))
            candidate = parsed.get("error", {}).get("code")
            if candidate in {"unsupported_country_region_territory", "insufficient_quota", "credit_balance_exhausted", "billing_not_active", "invalid_api_key", "model_not_found", "unsupported_value", "unsupported_parameter", "invalid_value", "missing_required_parameter", "context_length_exceeded"}:
                code = candidate
            param = parsed.get("error", {}).get("param")
            if isinstance(param, str) and re.fullmatch(r"[a-zA-Z0-9_.\[\]-]{1,100}", param):
                parameter = param
        except (ValueError, AttributeError):
            pass
        raise Failure(error.code if error.code in {400, 401, 403, 404, 429} else 502, code, parameter) from None
    except TimeoutError:
        raise Failure(502, "upstream_timeout") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise Failure(502, "upstream_timeout") from None
        raise Failure(502, "upstream_unavailable") from None
    except OSError:
        raise Failure(502, "upstream_unavailable") from None
    except ValueError:
        raise Failure(502, "upstream_invalid_json") from None


def respond(start_response, status, body, extra_headers=()):
    labels = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type", 429: "Too Many Requests", 502: "Bad Gateway", 503: "Service Unavailable"}
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
    if status in {429, 503}:
        headers.append(("Retry-After", "30"))
    headers.extend(extra_headers)
    start_response(f"{status} {labels.get(status, 'Error')}", headers)
    return [body]


def realtime_request(body, key, end=False):
    """Signalling only: microphone and synthesized audio travel over WebRTC."""
    try:
        raw = json.loads(body, object_pairs_hook=unique_object)
        if end:
            if set(raw) != {"call_id"} or not re.fullmatch(r"rtc_[A-Za-z0-9_-]{8,160}", raw["call_id"]):
                raise ValueError()
            request = urllib.request.Request("https://api.openai.com/v1/realtime/calls/" + raw["call_id"] + "/hangup",
                                             data=b"", headers={"Authorization": "Bearer " + key}, method="POST")
        else:
            if set(raw) != {"sdp", "instructions", "user"}:
                raise ValueError()
            sdp, instructions, user = raw["sdp"], raw["instructions"], raw["user"]
            if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > 48000 or "m=audio " not in sdp:
                raise ValueError()
            if not isinstance(instructions, str) or not 1 <= len(instructions) <= 24000 or not re.fullmatch(r"[a-f0-9]{64}", user):
                raise ValueError()
            session = {"type": "realtime", "model": "gpt-realtime-2.1-mini", "instructions": instructions,
                       "max_output_tokens": 600, "truncation": {"type": "retention_ratio", "retention_ratio": 0.8, "token_limits": {"post_instructions": 8000}}, "audio": {"input": {
                           "noise_reduction": {"type": "near_field"},
                           "transcription": {"model": "gpt-transcribe", "language": "ru"},
                           "turn_detection": {"type": "semantic_vad", "eagerness": "medium", "create_response": True, "interrupt_response": True}},
                           "output": {"voice": "cedar"}}}
            boundary = "academy-" + os.urandom(16).hex()
            parts = []
            for name, value in (("sdp", sdp), ("session", json.dumps(session, ensure_ascii=False))):
                parts.append(("--" + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + value + "\r\n").encode())
            encoded = b"".join(parts) + ("--" + boundary + "--\r\n").encode()
            request = urllib.request.Request("https://api.openai.com/v1/realtime/calls", data=encoded,
                        headers={"Authorization": "Bearer " + key, "Content-Type": "multipart/form-data; boundary=" + boundary,
                                 "OpenAI-Safety-Identifier": user}, method="POST")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Failure(400, "invalid_voice_payload") from None
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            answer = response.read(65537)
            if end:
                return json.dumps({"ended": True}).encode()
            call_id = response.headers.get("Location", "").rstrip("/").split("/")[-1]
            if len(answer) > 65536 or not answer.startswith(b"v=0") or not re.fullmatch(r"rtc_[A-Za-z0-9_-]{8,160}", call_id):
                raise Failure(502, "invalid_voice_answer")
            return json.dumps({"sdp": answer.decode(), "call_id": call_id, "model": session["model"]}).encode()
    except urllib.error.HTTPError as error:
        if end and error.code == 404:
            return b'{"ended":true}'
        code = "voice_upstream_error"
        try:
            candidate = json.loads(error.read(16384)).get("error", {}).get("code")
            if candidate in {"model_not_found", "insufficient_quota", "billing_not_active", "invalid_api_key", "unsupported_country_region_territory"}:
                code = candidate
        except (ValueError, AttributeError):
            pass
        raise Failure(error.code if error.code in {400, 401, 403, 404, 429} else 502, code) from None
    except (OSError, TimeoutError, ValueError):
        raise Failure(502, "voice_upstream_unavailable") from None


def run_job(job_id, payload, key):
    try:
        result = request_openai(payload, key)
        with _lock:
            _jobs[job_id].update(status="succeeded", result=result, finished=time.time())
        log.warning("job_completed job_id=%s response_bytes=%d", job_id, len(result))
    except Failure as error:
        with _lock:
            _jobs[job_id].update(status="failed", error=error, finished=time.time())
        log.warning("job_failed job_id=%s code=%s", job_id, error.code)
    except Exception:
        with _lock:
            _jobs[job_id].update(status="failed", error=Failure(502, "worker_error"), finished=time.time())
    finally:
        _slots.release()


def job_request(body, model, key, result_only=False):
    try:
        raw = json.loads(body, object_pairs_hook=unique_object)
        if not isinstance(raw, dict) or set(raw) != ({"job_id"} if result_only else {"job_id", "request"}):
            raise ValueError()
        job_id = raw["job_id"]
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise Failure(400, "invalid_payload") from None
    digest = hashlib.sha256(body).hexdigest()
    with _lock:
        for old in [j for j, v in _jobs.items() if v.get("finished", time.time()) < time.time() - JOB_TTL]:
            del _jobs[old]
        existing = _jobs.get(job_id)
        if existing:
            if not result_only and existing["digest"] != digest:
                raise Failure(409, "job_conflict")
            if result_only and existing["status"] == "succeeded":
                return 200, existing["result"]
            if result_only and existing["status"] == "failed":
                raise existing["error"]
            return 202, b'{"pending":true}'
        if result_only:
            raise Failure(404, "job_not_found")
    payload = payload_from(json.dumps(raw["request"]).encode(), model)
    with _lock:
        # Recheck after validation: concurrent duplicate submits must not pay twice.
        existing = _jobs.get(job_id)
        if existing:
            if existing["digest"] != digest:
                raise Failure(409, "job_conflict")
            return 202, b'{"pending":true}'
        if len(_jobs) >= 16 or not _slots.acquire(blocking=False):
            raise Failure(503, "worker_busy")
        _jobs[job_id] = {"status": "running", "digest": digest, "created": time.time()}
    try:
        threading.Thread(target=run_job, args=(job_id, payload, key), daemon=True).start()
    except Exception:
        with _lock:
            del _jobs[job_id]
        _slots.release()
        raise
    return 202, b'{"pending":true}'


def application(environ, start_response):
    acquired = False
    translation_acquired = False
    try:
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "")
        if path == "/healthz" and method == "GET":
            return respond(start_response, 200, b'{"status":"alive"}')
        if path == "/readyz" and method == "GET":
            settings()
            return respond(start_response, 200, b'{"status":"configured"}')
        if path not in {"/v1/analyze", "/v1/jobs", "/v1/jobs/result", "/v1/translate", "/v1/academy/realtime", "/v1/academy/realtime/end"}:
            raise Failure(404, "not_found")
        if method != "POST":
            raise Failure(405, "method_not_allowed")
        key, secret, model = settings()
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
            raise Failure(415, "json_required")
        length = environ.get("CONTENT_LENGTH", "")
        if not length.isdigit():
            raise Failure(411, "content_length_required")
        if len(length) > 9 or not 1 <= int(length) <= MAX_BODY:
            raise Failure(413, "payload_too_large")
        if path == '/v1/translate' and int(length)>65536:
            raise Failure(413,'payload_too_large')
        body = environ["wsgi.input"].read(int(length))
        if len(body) != int(length):
            raise Failure(400, "incomplete_body")
        authenticate(environ, body, secret, poll=path == "/v1/jobs/result")
        if path.startswith('/v1/academy/realtime'):
            if len(body) > 100000:
                raise Failure(413, 'payload_too_large')
            return respond(start_response, 200, realtime_request(body, key, end=path.endswith('/end')))
        if path == '/v1/translate':
            with _lock:
                if any(job.get('status')=='running' for job in _jobs.values()):raise Failure(503,'drawing_priority')
            translation_acquired=_translation_slot.acquire(blocking=False)
            if not translation_acquired:raise Failure(503,'translation_busy')
            acquired=_slots.acquire(blocking=False)
            if not acquired:raise Failure(503,'worker_busy')
            import free_translate
            try:
                texts=free_translate.validate(json.loads(body,object_pairs_hook=unique_object))
                translated=free_translate.translate(texts)
            except (ValueError,TypeError,KeyError):raise Failure(400,'invalid_translation_payload') from None
            return respond(start_response,200,json.dumps({'texts':translated,'engine':'opus-mt-en-ru','paid_api_calls':0},ensure_ascii=False).encode())
        if path != "/v1/analyze":
            status, result = job_request(body, model, key, result_only=path == "/v1/jobs/result")
            return respond(start_response, status, result)
        acquired = _slots.acquire(blocking=False)
        if not acquired:
            raise Failure(503, "worker_busy")
        payload = payload_from(body, model)
        started = time.monotonic()
        result = request_openai(payload, key)
        headers = [("X-Gosplan-Processing-Ms", str(round((time.monotonic() - started) * 1000)))]
        metrics = usage_metrics(result, model)
        if metrics is not None:
            headers.extend([
                ("X-Gosplan-Input-Tokens", str(metrics["input"])),
                ("X-Gosplan-Cached-Input-Tokens", str(metrics["cached"])),
                ("X-Gosplan-Output-Tokens", str(metrics["output"])),
            ])
            if metrics["estimated_usd"] is not None:
                headers.append(("X-Gosplan-Estimated-USD", "%.6f" % metrics["estimated_usd"]))
        return respond(start_response, 200, result, headers)
    except Failure as error:
        log.warning("request_failed status=%s code=%s", error.status, error.code)
        detail = {"code": error.code}
        if error.parameter:
            detail["param"] = error.parameter
        body = json.dumps({"error": detail}).encode()
        return respond(start_response, error.status, body)
    except Exception:
        return respond(start_response, 502, b'{"error":{"code":"worker_error"}}')
    finally:
        if acquired:
            _slots.release()
        if translation_acquired:
            _translation_slot.release()



