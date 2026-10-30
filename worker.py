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
_nonces = {}
_calls = []
log = logging.getLogger("gosplan")


class Failure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


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


def authenticate(environ, body, secret):
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
        _calls[:] = [t for t in _calls if now - t < 60]
        if nonce in _nonces:
            raise Failure(409, "replayed_request")
        if len(_nonces) >= 4096 or len(_calls) >= 10:
            raise Failure(429, "worker_rate_limit")
        _nonces[nonce] = now + 2 * WINDOW
        _calls.append(now)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate field")
        obj[key] = value
    return obj


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
        return {"model": model, "store": False, "instructions": instructions, "input": messages,
                "max_output_tokens": 12000, "text": {"format": {"type": "json_object"}}}
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise Failure(400, "invalid_payload") from None


def request_openai(payload, key):
    request = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=85) as response:
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise Failure(502, "upstream_response_too_large")
            result = json.loads(body)
            if not isinstance(result, dict) or result.get("status") != "completed":
                raise Failure(502, "upstream_incomplete")
            usage = result.get("usage", {})
            # Counts only: never log prompts, drawings, keys, or upstream error text.
            log.warning("openai_usage input=%s output=%s", usage.get("input_tokens"), usage.get("output_tokens"))
            return body
    except urllib.error.HTTPError as error:
        code = "upstream_error"
        try:
            parsed = json.loads(error.read(32768))
            candidate = parsed.get("error", {}).get("code")
            if candidate in {"unsupported_country_region_territory", "insufficient_quota", "credit_balance_exhausted", "billing_not_active", "invalid_api_key", "model_not_found"}:
                code = candidate
        except (ValueError, AttributeError):
            pass
        raise Failure(error.code if error.code in {400, 401, 403, 404, 429} else 502, code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Failure(502, "upstream_unavailable") from None
    except ValueError:
        raise Failure(502, "upstream_invalid_json") from None


def respond(start_response, status, body):
    labels = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type", 429: "Too Many Requests", 502: "Bad Gateway", 503: "Service Unavailable"}
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
    if status in {429, 503}:
        headers.append(("Retry-After", "30"))
    start_response(f"{status} {labels.get(status, 'Error')}", headers)
    return [body]


def application(environ, start_response):
    acquired = False
    try:
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "")
        if path == "/healthz" and method == "GET":
            return respond(start_response, 200, b'{"status":"alive"}')
        if path == "/readyz" and method == "GET":
            settings()
            return respond(start_response, 200, b'{"status":"configured"}')
        if path != "/v1/analyze":
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
        acquired = _slots.acquire(blocking=False)
        if not acquired:
            raise Failure(503, "worker_busy")
        body = environ["wsgi.input"].read(int(length))
        if len(body) != int(length):
            raise Failure(400, "incomplete_body")
        authenticate(environ, body, secret)
        result = request_openai(payload_from(body, model), key)
        return respond(start_response, 200, result)
    except Failure as error:
        body = json.dumps({"error": {"code": error.code}}).encode()
        return respond(start_response, error.status, body)
    except Exception:
        return respond(start_response, 502, b'{"error":{"code":"worker_error"}}')
    finally:
        if acquired:
            _slots.release()
