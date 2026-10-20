# Contributing

## Repository structure

- [docker/](docker/) contains the NFS server Dockerfile, startup script, and image changelog
- [helm/](helm/) contains the Helm chart, values, template tests, and chart changelog
- [docs/](docs/) contains the static Helm repository homepage
- [.justfiles/](.justfiles/) contains shared recipes for tooling, changelogs, Justfile formatting, and workflow linting
  The root [justfile](justfile) exposes these alongside the Docker and Helm modules
- [.changes/](.changes/) stores unreleased Changie fragments and versioned release notes
  [.changie.yaml](.changie.yaml) defines the image and chart projects
- [.github/workflows/](.github/workflows/) contains testing, release preparation, and publication workflows

Feature branches target `next`, where development changes accumulate.
Only this repository's `next` branch may open a PR into `main`.
Promotions into `main` must use a merge commit because release validation inspects both parents.

## Tools and setup

The [Brewfile](Brewfile) lists the local command-line tools. On macOS, install them from the repository root:

```sh
brew bundle
```

On other platforms, install the equivalent tools using your package manager.

- Bash runs the recipes, and Just provides the common commands
- Docker and Buildx support image checks and builds
- Helm and helm-unittest validate chart templates
- ShellCheck checks shell scripts
- Hadolint checks Dockerfiles
- Actionlint checks GitHub Actions workflows
- Changie manages release fragments and version bumps
- Git, GitHub CLI (`gh`), and `jq` support the development and release workflows

Docker commands require a running Docker runtime; installing the CLI alone does not start one.
The recipes check availability using `docker info`.
Docker linting does not require a running runtime.

`just helm test` installs helm-unittest if missing and replaces it if its version differs from the configured version
(currently `1.1.1`).
It then updates and builds chart dependencies before running the unit tests.
The plugin source, name, and version can be overridden through `HELM_UNITTEST_REPO`, `HELM_UNITTEST_NAME`, and
`HELM_UNITTEST_VERSION`.

## Development process

1. Create a feature branch from `next` and make the changes
2. Add or update chart tests in `helm/tests/` when changing template behavior
3. Add a Changie fragment for releasable image or chart changes as described below
4. Run the checks relevant to the change, then push and open a PR into `next`
5. Review the diff and merge after **Testing result** passes

Run commands from the repository root. Use `just --list --list-submodules` or just `just`
to discover the available recipes.

| Command             | Behavior                                                             |
| :------------------ | :------------------------------------------------------------------- |
| `just docker lint`  | Runs Bash syntax checks, ShellCheck, and Hadolint                    |
| `just docker test`  | Runs `docker build --check`; it does not build or run the NFS server |
| `just docker build` | Builds a local image tagged `nfsd:local`                             |
| `just helm lint`    | Runs Helm chart linting                                              |
| `just helm test`    | Prepares dependencies and runs helm-unittest                         |
| `just github lint`  | Runs Actionlint with ShellCheck on workflow scripts                  |
| `just just lint`    | Checks Justfile formatting                                           |
| `just fmt`          | Formats Justfiles                                                    |
| `just lint`         | Runs all lint checks                                                 |
| `just test`         | Runs Dockerfile checks and Helm unit tests                           |
| `just testolint`    | Runs all lint checks, then tests                                     |

These checks validate build definitions and chart templates.
They do not deploy the chart or test a running NFS server.

### CI behavior

The **Testing** workflow runs on PRs into `next` and `main`, pushes to `main`, and manual dispatch.
It selects workflow, Justfile, Docker, and Helm checks based on changed paths.
PRs into `main`, manual runs, and changes to the testing workflow select all checks.

The final **Testing result** job combines branch policy, lint, and test results.
It permits unselected tests to be skipped, but requires selected checks to pass.

## Changes and versioning

Create a fragment with:

```sh
just change new
```

Select the `image` project for Docker image changes and `chart` for Helm chart changes.
Commit the resulting fragment in `.changes/unreleased/` with the source change.
Choose the kind according to its effect:

- `Breaking` produces a major bump
- `Added` produces a minor bump
- `Changed`, `Fixed`, and `Security` produce a patch bump

Release preparation batches the fragments and updates the changelogs and versions.
Leave those updates to the preparation workflow rather than manually bumping versions during development.

In `helm/Chart.yaml`, `appVersion` identifies the Docker image version and `version` identifies the chart version.
An image release automatically adds a chart fragment to update the image reference.
A chart-only release bumps the chart version while retaining the image version.
The chart uses `appVersion` as its default image tag; users can override it through `image.tag`.

## Release process

1. Merge the intended source changes and fragments into `next`
2. Open **Actions → Prepare release → Run workflow** and select `main`.
   The workflow runs from `main` but prepares the contents of `next`.
3. Review the generated preparation PR into `next`.
   It contains updated versions, changelogs, versioned release notes, and `.changes/release.json`, which records
   the source commit, previous and new versions, and whether the image changed.
   The workflow creates the commit through GitHub's API and verifies its signature before opening the PR.
   Keep these files consistent if editing the preparation PR.
4. Approve workflows on the bot-created PR if GitHub requests it, and merge after **Testing result** passes.
5. Open a PR from `next` into `main`.
   Wait for the full checks and use **Create a merge commit**.
   Squash and rebase merges do not satisfy release validation.
6. Watch **Releasing** validate the preparation, publish the artifacts, and deploy the Helm repository site through Pages.
   Leave `publish_existing` disabled for this process.

Preparation creates no PR when there are no unreleased fragments.
If a preparation PR is already open, the workflow preserves it rather than creating another one.
If `next` gains Docker source changes after preparation,
regenerate preparation from the updated source before promotion, preserving deliberate manual edits.
Re-running preparation while its PR remains open does not regenerate it.

Publication uses the exact promotion commit.
It:

1. Checks the prepared source and versions
2. Builds changed images on native amd64 and arm64 runners
3. Pushes each platform by digest
4. Combines them under the version tag

Chart-only releases skip the image builds.
Docker and Helm authenticate to the Nebius registry using the Nebius CLI credential helper in CI.

### Published artifacts

- Docker image:
  `<NEBIUS_REGISTRY_HOST>/<NEBIUS_REGISTRY_NAMESPACE>/nfsd:<appVersion>`,
  containing both `linux/amd64` and `linux/arm64` platforms
- Helm OCI chart:
  `oci://<NEBIUS_REGISTRY_HOST>/<NEBIUS_REGISTRY_NAMESPACE>/nfs-server`,
  selected with `--version <version>`
- GitHub image release: `image-v<appVersion>`,
  containing release notes and the image reference
- GitHub chart release: `nfs-server-<version>`,
  with the packaged chart archive attached
- Helm repository index: `index.yaml` committed to `gh-pages` and deployed through GitHub Pages

The [Helm repository homepage](https://nebius.github.io/nfs-server/) provides installation commands and a link to the index.
Its source is [docs/index.html](docs/index.html), a static HTML page with no build step.
Publication copies it to `gh-pages` alongside `index.yaml`, committing changes to either file.
Homepage edits deploy with the next release; the Pages job serves the exact published snapshot.
