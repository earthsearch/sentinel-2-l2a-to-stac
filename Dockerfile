FROM ghcr.io/astral-sh/uv:0.6.6 AS uv

FROM ghcr.io/lambgeo/lambda-gdal:3.8-python3.11 as gdal

FROM public.ecr.aws/lambda/python:3.11 as builder

# Bring C libs from lambgeo/lambda-gdal image
COPY --from=gdal /opt/lib/ ${LAMBDA_TASK_ROOT}/lib/
COPY --from=gdal /opt/include/ ${LAMBDA_TASK_ROOT}/include/
COPY --from=gdal /opt/share/ ${LAMBDA_TASK_ROOT}/share/
COPY --from=gdal /opt/bin/ ${LAMBDA_TASK_ROOT}/bin/

ENV \
  GDAL_DATA=${LAMBDA_TASK_ROOT}/share/gdal \
  PROJ_LIB=${LAMBDA_TASK_ROOT}/share/proj \
  GDAL_CONFIG=${LAMBDA_TASK_ROOT}/bin/gdal-config \
  GEOS_CONFIG=${LAMBDA_TASK_ROOT}/bin/geos-config \
  PATH=${LAMBDA_TASK_ROOT}/bin:$PATH

RUN yum update -y && \
  yum install -y git libxml2-devel libxslt-devel python-devel gcc && \
  yum clean all && \
  rm -rf /var/cache/yum /var/lib/yum/history

# Enable bytecode compilation, to improve cold-start performance.
ENV UV_COMPILE_BYTECODE=1

# Disable installer metadata, to create a deterministic layer.
ENV UV_NO_INSTALLER_METADATA=1

# Enable copy mode to support bind mount caching.
ENV UV_LINK_MODE=copy

# Bundle the dependencies into the Lambda task root via `uv pip install --target`.
#
# Omit any local packages (`--no-emit-workspace`) and development dependencies (`--no-dev`).
# This ensures that the Docker layer cache is only invalidated when the `pyproject.toml` or `uv.lock`
# files change, but remains robust to changes in the application code.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=README.md,target=README.md \
    --mount=type=bind,source=src,target=src \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv export --frozen --no-emit-workspace --no-dev --no-editable -o requirements.txt && \
    uv pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}" .


FROM public.ecr.aws/lambda/python:3.11

# Copy the runtime dependencies from the builder stage.
COPY --from=builder ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}

COPY src/cirrus_task_example/ ${LAMBDA_TASK_ROOT}/cirrus_task_example/

WORKDIR ${LAMBDA_TASK_ROOT}

# Uncomment one of the following:

# 1. for lambda task, use CMD
CMD [ "cirrus_task_example.task.lambda_handler" ]

# 2. for batch task, use ENTRYPOINT
#ENV PYTHONPATH="/var/task"
#ENTRYPOINT [ "./bin/cirrus-task-example" ]
