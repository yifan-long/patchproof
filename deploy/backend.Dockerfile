# PatchProof backend image (方案 A 部署)
# Build context: repository root
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime deps via the project's pyproject (hatchling build backend).
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8010

# data/ is mounted from the host for SQLite + run workspaces.
CMD ["uvicorn", "patchproof.api:app", "--host", "0.0.0.0", "--port", "8010"]
