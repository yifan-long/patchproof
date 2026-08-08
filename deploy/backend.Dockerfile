# Build context: repository root
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Keep the source layout expected by patchproof.config so runtime data is under
# /workspace/patchproof/data instead of site-packages.
WORKDIR /workspace/patchproof

COPY pyproject.toml ./
COPY src ./src
COPY benchmarks ./benchmarks
RUN pip install --no-cache-dir .

ENV PYTHONPATH=/workspace/patchproof/src

EXPOSE 8010

# data/ and user repositories are mounted by Compose.
CMD ["uvicorn", "patchproof.api:app", "--app-dir", "/workspace/patchproof/src", "--host", "0.0.0.0", "--port", "8010"]
