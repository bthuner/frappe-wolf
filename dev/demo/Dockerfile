# Demo image: a Dokos bench with frappe_wolf installed and the Wolf agent in the bench env.
# Build context is the repository root. The base defaults to a local Dokos image; any
# registry.gitlab.com/dokos/dokos tag works too.
ARG BASE=registry.gitlab.com/dokos/dokos:latest
FROM ${BASE}

USER frappe
WORKDIR /home/frappe/frappe-bench
# The Wolf UI's packages first: this layer survives edits to the Python app.
COPY --chown=frappe:frappe frontend/package.json frontend/yarn.lock apps/frappe_wolf/frontend/
RUN cd apps/frappe_wolf/frontend && yarn install --frozen-lockfile --non-interactive
COPY --chown=frappe:frappe pyproject.toml README.md LICENSE package.json apps/frappe_wolf/
COPY --chown=frappe:frappe frappe_wolf apps/frappe_wolf/frappe_wolf
# The Wolf UI (/wolf): what `bench build --app frappe_wolf` runs, without rebuilding Desk assets.
COPY --chown=frappe:frappe frontend apps/frappe_wolf/frontend
RUN cd apps/frappe_wolf/frontend && yarn build
COPY --chown=frappe:frappe agent /home/frappe/wolf-agent-src
RUN env/bin/pip install --no-cache-dir -e apps/frappe_wolf \
 && env/bin/pip install --no-cache-dir /home/frappe/wolf-agent-src \
 && ln -sfn /home/frappe/frappe-bench/apps/frappe_wolf/frappe_wolf/public assets/frappe_wolf
# The demo MariaDB has no TLS; newer mariadb clients require it by default.
RUN printf '[client]\nskip-ssl\n' > /home/frappe/.my.cnf
# Owned by frappe so a named volume mounted here inherits the ownership.
RUN mkdir -p /home/frappe/wolf-agent/spool

# Optional: Raven (backend only, no frontend build) to exercise the Raven alert channel.
# blurhash-python (image previews) has no wheel for Python 3.14, so Raven goes in without it.
ARG WITH_RAVEN=0
ARG RAVEN_REF=main
RUN if [ "$WITH_RAVEN" = 1 ]; then \
      git clone --depth 1 --branch "$RAVEN_REF" https://github.com/The-Commit-Company/raven apps/raven \
      && env/bin/pip install --no-cache-dir --no-deps -e apps/raven \
      && env/bin/pip install --no-cache-dir linkpreview openai \
      && ln -sfn /home/frappe/frappe-bench/apps/raven/raven/public assets/raven; \
    fi
