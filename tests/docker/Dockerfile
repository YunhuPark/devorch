# Test image for tests/test_docker_sandbox.py: python + pytest only. The sandbox runs without network, so dependencies must already be inside.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest
WORKDIR /work
