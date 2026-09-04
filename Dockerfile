FROM ghcr.io/astral-sh/uv:0.6.6 AS uv

FROM ghcr.io/lambgeo/lambda-gdal:3.10-python3.12 as gdal

FROM public.ecr.aws/lambda/python:3.12 as builder

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

# Install directly from uv.lock into the Lambda task root. --frozen means uv reads
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
    uv pip install --frozen --no-editable --target "${LAMBDA_TASK_ROOT}" .


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
