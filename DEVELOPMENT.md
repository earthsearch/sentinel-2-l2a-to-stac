# Cirrus Task Development

This repo builds a containerized Cirrus task, using the [stac-task](https://github.com/stac-utils/stac-task) library.
The stac-task Task class has several convenient utility methods for performing common tasks
(e.g., setting collection, uploading STAC Item assets to s3).

See the [stac-task](https://github.com/stac-utils/stac-task) README for more information on developing a task.

## Template Usage

To use this template as a repository:

- Update `pyproject.toml` with the project name, authors, description, dependencies, etc.
- Rename the folder `src/cirrus_task_example` to match the project name
- Add test payload fixtures under `tests/fixtures`
- Add code to `src/<project-name>/task.py` and add supporting files as needed
- Update the Repostirory URL in CHANGELOG.md and keep it up to date with versions
- Provide a Dockerfile - use the one provided or create your own. `task.py` must be in the $PATH

## Deployment

The built Docker image from this code should be published to a registry that is accessible
from the Cirrus deployment. Ideally the image is in the same region as where the task will be run. 
