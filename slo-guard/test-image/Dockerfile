# KubeLLM-Ops 테스트 전용 이미지 — Claude dev-supervisor `--docker-image`와 Codex commander `--approve-docker` 공용.
# 두 도구 모두 이미지를 받거나 패키지를 설치하지 않으므로, 이 이미지를 미리 빌드하고 digest로 고정해 쓴다.
# 테스트는 --network none, --read-only 컨테이너에서 돌기 때문에 패키지는 빌드 때 모두 설치한다.
# supervisor는 실행 사용자를 지정하지 않으므로 supervisor-py:1과 같은 일반 사용자(runner, uid 10001)로 실행한다.
FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements-test.txt /tmp/requirements-test.txt
RUN pip install -r /tmp/requirements-test.txt && rm /tmp/requirements-test.txt \
    && useradd -m -u 10001 runner
USER runner
