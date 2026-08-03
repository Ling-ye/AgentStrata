# Third-party notices

AgentStrata source code and documentation are licensed under the repository's
MIT License unless a file states otherwise.

## Repository inventory

The first public release candidate does not vendor third-party source trees,
fonts, compiled libraries, executables, model weights, or datasets.

The repository declares dependencies and optional runtime services that are
downloaded separately:

- Python runtime dependencies are declared in `pyproject.toml`.
- The release-only, hash-locked Python build closure is declared in
  `requirements/release-build.txt`; those tools are downloaded during CI
  and are not redistributed by AgentStrata.
- Console JavaScript dependencies are declared in `console/web/package.json`
  and locked by `console/web/package-lock.json`.
- Optional OCI images and MCP packages are declared in
  `deploy/docker/docker-compose.yaml`.

Those components remain under their respective upstream licenses and are not
relicensed by AgentStrata. Container images are not embedded in this repository
or in the Python wheel. Operators and redistributors must review the license and
distribution terms for the exact versions they choose to install.

## Development and release tooling

Repository checks download the official Gitleaks 8.30.1 Linux archive at run
time and verify its pinned SHA-256 digest; Gitleaks is not redistributed by
AgentStrata. GitHub workflows reference official Actions by immutable commit
SHA. Those tools and Actions remain under their respective upstream licenses.

## Documentation adaptation

`CODE_OF_CONDUCT.md` is a condensed and modified adaptation of Contributor
Covenant 2.1, which is available under the Creative Commons Attribution 4.0
International license. The project version changes the scope, reporting, and
enforcement language. The upstream text and attribution are linked from the
adapted file.

## Release maintenance

Before each public release:

1. inventory tracked binary and archive formats;
2. inspect newly copied or modified third-party source and media;
3. verify license and attribution requirements for packaged artifacts;
4. inspect dependency and container-image license changes; and
5. update this file when AgentStrata redistributes third-party material.
