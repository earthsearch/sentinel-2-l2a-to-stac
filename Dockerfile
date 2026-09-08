FROM ghcr.io/astral-sh/uv:0.6.6 AS uv

FROM public.ecr.aws/lambda/python:3.12 AS builder

# No system GDAL needed: the pinned rasterio (1.5.x) and pyproj (3.7.x) wheels are
# manylinux_2_28 and bundle their own GDAL/PROJ/GEOS. The Lambda base is Amazon
# Linux 2023 (glibc 2.34), which satisfies manylinux_2_28, so the wheels install
# and run as-is. `task.py` uses rasterio only (no osgeo bindings), so there is
# nothing that needs a system libgdal. `git` is required because pystac is a
# git dependency (see [tool.uv.sources] in pyproject.toml).
RUN dnf update -y && \
  dnf install -y git && \
  dnf clean all && \
  rm -rf /var/cache/dnf

# Enable bytecode compilation, to improve cold-start performance.
ENV UV_COMPILE_BYTECODE=1

# Disable installer metadata, to create a deterministic layer.
ENV UV_NO_INSTALLER_METADATA=1

# Enable copy mode to support bind mount caching.
ENV UV_LINK_MODE=copy

# Install directly from uv.lock into the Lambda task root. uv export --frozen reads
# exact versions from uv.lock without regenerating it; dev dependencies are excluded
# because they are not part of [project.dependencies]. The Docker layer cache is only
# invalidated when pyproject.toml or uv.lock change; source changes land via the
# COPY at the bottom of the final stage without re-installing dependencies.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=README.md,target=README.md \
    --mount=type=bind,source=src,target=src \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv export --frozen --no-dev | \
    uv pip install -r /dev/stdin --target "${LAMBDA_TASK_ROOT}"


FROM public.ecr.aws/lambda/python:3.12

# Copy the runtime dependencies from the builder stage.
COPY --from=builder ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}

COPY src/sentinel_2_l2a_to_stac/ ${LAMBDA_TASK_ROOT}/sentinel_2_l2a_to_stac/

WORKDIR ${LAMBDA_TASK_ROOT}

# Uncomment one of the following:

# 1. for lambda task, use CMD
CMD [ "sentinel_2_l2a_to_stac.task.lambda_handler" ]

# 2. for batch task, use ENTRYPOINT
#ENV PYTHONPATH="/var/task"
#ENTRYPOINT [ "./bin/sentinel-2-l2a-to-stac" ]
