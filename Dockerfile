FROM python:3.12-slim AS model-builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY build_translation_model.py free_translate.py ./
RUN python build_translation_model.py && python -c "from free_translate import translate; result=translate(['A new design exhibition opens today.'])[0]; assert any(chr(1040)<=c<=chr(1103) for c in result), result; print('Offline translation smoke test passed')"

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home worker
COPY worker.py free_translate.py ./
COPY --from=model-builder /opt/inspiration-enru /opt/inspiration-enru
USER worker
EXPOSE 10000
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4", "--timeout", "300", "--error-logfile", "-", "worker:application"]

