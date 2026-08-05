ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    TZ=UTC

COPY requirements.lock /opt/patchproof/requirements.lock
RUN python -m pip install --no-cache-dir -r /opt/patchproof/requirements.lock

WORKDIR /workspace
