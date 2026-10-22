# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:2259debdc6af85626058a15a7bf44ad6a68b8c98c9ae82a02cf5cdb92f096d47 AS base

# 컨테이너 안에서 불필요한 캐시/바이트코드 파일을 만들지 않도록 설정
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY app ./app

# root가 아닌 전용 사용자로 실행한다 (컨테이너가 뚫려도 root 권한을 주지 않기 위해)
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 컨테이너 자체의 healthcheck. Kubernetes probe와는 별개로, docker run 단독 실행 시에도 상태를 볼 수 있게 한다.
# Docker health observes readiness; unhealthy alone does not restart a Docker container.
# Kubernetes liveness continues to use the dependency-independent /healthz endpoint.
HEALTHCHECK --interval=10s --timeout=4s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/readyz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
