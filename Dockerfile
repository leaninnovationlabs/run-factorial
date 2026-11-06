# The whole app in one image (tech-stack decision 52): Postgres, migrations, the API (which serves
# the dashboard), the worker, the terminal gateway and the assistant under s6-overlay, with git,
# gh, glab, Node, tmux and the claude and codex CLIs, so the worker runs claude_cli and codex_cli
# agents inside the container.
# `make image` builds it for a developer machine. A deployment can build, scan and push it to a
# registry and run it as one task on an external Postgres (decision 64). That image is fit to
# publish as it is (decision 84): the build context is an allowlist (.dockerignore), and the
# Python it ships has no comments. `make up` runs it the same way, beside its own Postgres in a
# second container (docker-compose.yml, decision 73): /etc/factorial/database.sh says how the image tells them apart.
#
# Stop it with `docker stop`, not `docker rm -f`: s6 waits for each service, and the terminal
# gateway needs up to 45 s to close the tmux clients it attached; one it leaves in an E2B sandbox
# stays attached. docker-compose.yml gives the app 60 s, as Docker's default 10 s is too short,
# and the ECS task's stopTimeout is 120 s.
#
# Configuration reaches the services as FILES: an agent run inherits the worker's process
# environment, so an exported FACTORIAL_MASTER_KEY would reach every agent. It arrives as a mount,
# `.env` at /run/secrets/factorial.env, or, on ECS, as FACTORIAL_* variables with
# FACTORIAL_MASTER_KEY among them. The `init` service writes the variables to that path and
# removes them from the environment the services start from, then copies the file to one the
# service user owns; the startup check refuses to start while a secret is still in that
# environment. With the mount, pass no secret with `-e` or `--env-file`: init moves variables only
# when the master key arrives as one, and stops, naming them, when it arrives beside the mount.
# Under compose the mount is its secret. GitHub credentials are each project's, encrypted in the
# database.
#
# Packaging, not containment: agents, the API and the worker share one filesystem. The one boundary
# drawn inside it is the assistant's: it runs as `factorial-assistant`, which cannot read `.env`
# (design.md §7.18; the `assistant` service and `init`).
#
# codex_cli runs here: its sandbox needs user namespaces that Docker's default seccomp profile
# refuses, so docker-compose.yml applies docker/seccomp.json (decision 52). A plain `docker run`
# without --security-opt and the ECS task do not apply it.
# orchestrator/tests/test_image.py fails when the claude version in docker/clis/ or CODEX_VERSION
# drifts from its adapter's PINNED_VERSION, and when GLAB_VERSION drifts from the E2B template's.
#
# Every input is pinned to its bytes, so a re-pushed tag or a changed package cannot enter an
# image without a change here: each base image by digest (the tag beside it is for the reader;
# bump both together), the claude CLI by docker/clis/'s lockfile, pnpm by PNPM_INTEGRITY (`npm
# view pnpm@<version> dist.integrity`), and Codex, s6-overlay and glab by their release checksums.

ARG NODE_IMAGE=node:22.23.4-trixie-slim@sha256:2117f713a417f3aff682ac8f5bc2ebbdf1beed51f36e67ab1e0fd3866b6d2d8f
ARG PYTHON_IMAGE=python:3.13-slim-trixie@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff
ARG CODEX_VERSION=0.164.0
ARG PNPM_VERSION=11.23.0
ARG PNPM_INTEGRITY=sha512-8ACC5bKDoZm3Tgedoo0VXACP4jL0TIoG6n3foBTs9xn8Ni95DsxnsoEG3awq+yTEmpoHkVnaVgss59Hpjv0Rrw==
ARG S6_OVERLAY_VERSION=3.2.3.2
ARG GLAB_VERSION=1.123.0

FROM ${UV_IMAGE} AS uv

FROM ${NODE_IMAGE} AS dashboard
ARG PNPM_VERSION
ARG PNPM_INTEGRITY
# pnpm has no dependencies, so its one tarball, checked, is all of it.
RUN cd /tmp \
 && npm pack --silent "pnpm@${PNPM_VERSION}" > /dev/null \
 && node -e ' \
      const [file, want] = process.argv.slice(1); \
      const got = "sha512-" + require("crypto").createHash("sha512") \
        .update(require("fs").readFileSync(file)).digest("base64"); \
      if (got !== want) { console.error(`${file} is ${got}, not ${want}`); process.exit(1); }' \
      "pnpm-${PNPM_VERSION}.tgz" "$PNPM_INTEGRITY" \
 && npm install -g --offline "./pnpm-${PNPM_VERSION}.tgz" \
 && rm "pnpm-${PNPM_VERSION}.tgz"
WORKDIR /ui
COPY ui/package.json ui/pnpm-lock.yaml ui/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ ./
# The API's origin, compiled into the bundle; the code adds `/factorial`. Empty is the dashboard's
# own origin, which serves the API. The terminal's origin is not built in: the API names it at
# run time (`FACTORIAL_TERMINAL__PUBLIC_URL`), so one image serves every shape.
ARG VITE_API_BASE_URL=
# Vite copies `public/` with the checkout's modes.
RUN VITE_API_BASE_URL="$VITE_API_BASE_URL" pnpm build \
 && chmod -R u=rwX,go=rX dist

# Each stage that takes files from the checkout gives them modes of its own, and the final stage
# copies a single file with --chmod: git writes a file with the cloner's umask, and under 077 no
# service user could read one, so the container would stop at its startup check on a
# PermissionError that names no cause.

# The Python source the image ships, without its comments (decision 84). A stage of its own, so
# the script stays out of the image. compileall catches what an AST comparison cannot.
FROM ${PYTHON_IMAGE} AS source
WORKDIR /src
COPY alembic.ini ./
COPY orchestrator/src orchestrator/src
COPY orchestrator/migrations orchestrator/migrations
COPY runtime/src runtime/src
COPY docker/strip-comments.py /tmp/strip-comments.py
RUN python -I /tmp/strip-comments.py orchestrator/src orchestrator/migrations runtime/src \
 && python -I -m compileall -q orchestrator/src orchestrator/migrations runtime/src \
 && chmod -R u=rwX,go=rX /src

# The s6 tree and /etc/factorial: the scripts keep their execute bit, and nothing else of the
# checkout's modes.
FROM ${PYTHON_IMAGE} AS rootfs
COPY docker/rootfs/ /rootfs/
COPY docker/seccomp.json /rootfs/etc/factorial/seccomp.json
RUN chmod -R u=rwX,go=rX /rootfs

FROM ${NODE_IMAGE} AS clis
WORKDIR /opt/clis
COPY --chmod=0644 docker/clis/package.json docker/clis/package-lock.json ./
RUN npm ci --no-audit --no-fund \
 && mkdir bin \
 && ln -s ../node_modules/.bin/claude bin/claude

# The native release build, not the npm package: that is a Node wrapper around this same binary,
# and a SIGKILL on the wrapper orphans the native process. The package keeps the layout the binary
# finds its bundled `rg` and `bwrap` by.
FROM ${PYTHON_IMAGE} AS codex
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
ARG CODEX_VERSION
RUN case "$TARGETARCH" in \
      amd64) triple=x86_64-unknown-linux-musl ;; \
      arm64) triple=aarch64-unknown-linux-musl ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && base="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}" \
 && package="codex-package-$triple.tar.gz" \
 && cd /tmp \
 && curl -fsSLO "$base/$package" \
 && curl -fsSLO "$base/codex-package_SHA256SUMS" \
 && grep " $package\$" codex-package_SHA256SUMS | sha256sum -c - \
 && mkdir /opt/codex \
 && tar -C /opt/codex --no-same-owner -xzf "$package" \
 && rm "$package" codex-package_SHA256SUMS \
 && test "$(/opt/codex/bin/codex --version)" = "codex-cli ${CODEX_VERSION}"

FROM ${PYTHON_IMAGE}
ARG S6_OVERLAY_VERSION
ARG GLAB_VERSION
ARG TARGETARCH

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    PATH="/app/.venv/bin:/opt/clis/bin:/opt/codex/bin:$PATH"

# Debian's package would create and start a cluster of its own; `init-db` makes /data/postgres.
# Its ssl-cert dependency makes a snakeoil key pair at install, which every copy of a published
# image would share; `initdb` leaves ssl off, so nothing reads it.
# ca-certificates 20250419+deb13u1 distrusts the roots the compromised eMudhra intermediates chain
# to. A cached base layer can predate it, so a stale build fails here rather than ship.
RUN mkdir -p /etc/postgresql-common/createcluster.conf.d \
 && echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.conf.d/factorial.conf \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gh git postgresql-17 tmux xz-utils \
 && rm -rf /var/lib/apt/lists/* \
 && rm /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem \
 && v="$(dpkg-query -W -f='${Version}' ca-certificates)" \
 && { dpkg --compare-versions "$v" ge 20250419+deb13u1 \
      || { echo "ca-certificates $v predates 20250419+deb13u1: build with --pull" >&2; exit 1; }; }

RUN case "$TARGETARCH" in \
      amd64) arch=x86_64 ;; \
      arm64) arch=aarch64 ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}" \
 && cd /tmp \
 && for part in noarch "$arch"; do \
      curl -fsSLO "$base/s6-overlay-$part.tar.xz" \
   && curl -fsSLO "$base/s6-overlay-$part.tar.xz.sha256" \
   && sha256sum -c "s6-overlay-$part.tar.xz.sha256" \
   && tar -C / -Jxpf "s6-overlay-$part.tar.xz" \
   && rm "s6-overlay-$part.tar.xz" "s6-overlay-$part.tar.xz.sha256" \
   || exit 1; \
    done

# The GitLab starter agents open merge requests with glab. A temporary HOME for the version check
# keeps root's glab config out of the image.
RUN case "$TARGETARCH" in \
      amd64|arm64) ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && base="https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads" \
 && tarball="glab_${GLAB_VERSION}_linux_${TARGETARCH}.tar.gz" \
 && cd /tmp \
 && curl -fsSLO "$base/$tarball" \
 && curl -fsSLO "$base/checksums.txt" \
 && grep " $tarball\$" checksums.txt | sha256sum -c - \
 && member="$(tar -tzf "$tarball" | grep -E '(^|/)glab$' || true)" \
 && { [ -n "$member" ] && [ "$(printf '%s\n' "$member" | wc -l)" -eq 1 ] \
      || { echo "expected one glab in $tarball, found: $member" >&2; exit 1; }; } \
 && tar -xzf "$tarball" -O "$member" > /usr/local/bin/glab \
 && chmod 0755 /usr/local/bin/glab \
 && rm "$tarball" checksums.txt \
 && test "$(command -v glab)" = /usr/local/bin/glab \
 && home="$(mktemp -d)" \
 && HOME="$home" glab --version | grep -qF "$GLAB_VERSION" \
 && rm -rf "$home"

# npm and npx stay because a stdio MCP server in `.factorial/mcp/` is typically launched with npx.
COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=clis /opt/clis /opt/clis
COPY --from=codex /opt/codex /opt/codex
COPY --from=uv /uv /usr/local/bin/uv

# Fixed ids, because /data outlives the image: on EFS, and in a copy of it, an owner is a number.
RUN useradd --create-home --uid 10001 factorial \
 && groupadd --system --gid 10002 factorial-assistant \
 && useradd --system --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin factorial-assistant \
 && install -d -o factorial-assistant -g factorial-assistant -m 0700 /data/assistant

WORKDIR /app

# Dependency layer: only the manifests, so edits to source do not re-resolve.
COPY --chmod=0644 pyproject.toml uv.lock ./
COPY --chmod=0644 orchestrator/pyproject.toml orchestrator/
COPY --chmod=0644 runtime/pyproject.toml runtime/
# claude-agent-sdk's wheel bundles a `claude` of 240 MB, the same version as /opt/clis's. When the
# two are the same bytes, the bundled one becomes a link to the other; when they differ, both stay.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace \
 && bundled="$(echo /app/.venv/lib/python3.*/site-packages/claude_agent_sdk/_bundled/claude)" \
 && real=/opt/clis/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
 && if cmp -s "$bundled" "$real"; then ln -sf "$real" "$bundled"; fi

COPY --from=source /src/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

COPY --from=dashboard /ui/dist /app/ui/dist
# With /etc/factorial/seccomp.json, for `docker run --security-opt seccomp=...`: a person who
# pulled the image has no checkout.
COPY --from=rootfs /rootfs/ /
COPY --chmod=0644 LICENSE NOTICE /app/

# Last, so a new commit rebuilds no layer above. No `source` label: the source repository is
# private.
ARG REVISION=
RUN printf '%s\n' "$REVISION" > /etc/factorial/revision
LABEL org.opencontainers.image.title="factorial" \
      org.opencontainers.image.description="GitHub issues through an agent development life cycle: AI agents do each step, people approve at gates" \
      org.opencontainers.image.licenses="FSL-1.1-ALv2" \
      org.opencontainers.image.url="https://www.runfactorial.com" \
      org.opencontainers.image.documentation="https://www.runfactorial.com/docs/run-the-public-image/" \
      org.opencontainers.image.vendor="Lean Innovation Labs LLC" \
      org.opencontainers.image.revision="${REVISION}"

EXPOSE 8000 8001
VOLUME /data
ENTRYPOINT ["/init"]
