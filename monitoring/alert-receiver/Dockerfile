FROM kubellm-ops-gateway:operations-20261021
USER root
RUN mkdir -p /data && chown 10001:10001 /data
COPY app.py /app/alert_receiver.py
USER appuser
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"
CMD ["uvicorn", "alert_receiver:app", "--host", "0.0.0.0", "--port", "8000"]
