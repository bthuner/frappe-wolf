# Demo image: a Dokos bench with frappe_wolf installed and the Wolf agent in the bench env.
# Build context is the repository root. The base defaults to a local Dokos image; any
# registry.gitlab.com/dokos/dokos tag works too.
ARG BASE=registry.gitlab.com/dokos/dokos:latest
FROM ${BASE}

USER frappe
WORKDIR /home/frappe/frappe-bench
COPY --chown=frappe:frappe pyproject.toml README.md LICENSE apps/frappe_wolf/
COPY --chown=frappe:frappe frappe_wolf apps/frappe_wolf/frappe_wolf
COPY --chown=frappe:frappe agent /home/frappe/wolf-agent-src
RUN env/bin/pip install --no-cache-dir -e apps/frappe_wolf \
 && env/bin/pip install --no-cache-dir /home/frappe/wolf-agent-src \
 && ln -sfn /home/frappe/frappe-bench/apps/frappe_wolf/frappe_wolf/public assets/frappe_wolf
# The demo MariaDB has no TLS; newer mariadb clients require it by default.
RUN printf '[client]\nskip-ssl\n' > /home/frappe/.my.cnf
# Owned by frappe so a named volume mounted here inherits the ownership.
RUN mkdir -p /home/frappe/wolf-agent/spool
