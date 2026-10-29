#!/usr/bin/env bash
# Integration check, run inside a Dokos bench image (registry.gitlab.com/dokos/dokos).
# Installs the checked-out frappe_wolf (mounted read-only at /src) into the image's bench,
# creates a site on the given database and runs ci/db_smoke.py on it.
#
#   ci/integration.sh mariadb|postgres
#
# Expects the database at host "db" (root password "admin") and Redis at host "redis".
set -euo pipefail

DB_TYPE="${1:?usage: integration.sh mariadb|postgres}"
SRC="${WOLF_SRC:-/src}"
BENCH="${BENCH_DIR:-/home/frappe/frappe-bench}"
SITE="ci.localhost"

cd "$BENCH"
echo "::group::Install frappe_wolf into the bench"
mkdir -p apps/frappe_wolf
cp -r "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/LICENSE" "$SRC/frappe_wolf" apps/frappe_wolf/
env/bin/pip install --quiet --no-cache-dir -e apps/frappe_wolf
ln -sfn "$BENCH/apps/frappe_wolf/frappe_wolf/public" assets/frappe_wolf
ls -1 apps > sites/apps.txt
[ -e sites/assets ] || ln -s "$BENCH/assets" sites/assets
# The CI MariaDB has no TLS; newer mariadb clients require it by default.
printf '[client]\nskip-ssl\n' > "$HOME/.my.cnf"
cat sites/apps.txt
echo "::endgroup::"

echo "::group::Configure bench"
bench set-config -g db_host db
bench set-config -g redis_cache redis://redis:6379
bench set-config -g redis_queue redis://redis:6379
bench set-config -g redis_socketio redis://redis:6379
echo "::endgroup::"

echo "::group::Create site on $DB_TYPE"
case "$DB_TYPE" in
	mariadb)
		bench new-site "$SITE" --db-type mariadb --mariadb-user-host-login-scope='%' \
			--db-root-username root --db-root-password admin --admin-password admin \
			--install-app frappe_wolf
		;;
	postgres)
		bench new-site "$SITE" --db-type postgres \
			--db-root-username postgres --db-root-password admin --admin-password admin \
			--install-app frappe_wolf
		;;
	*)
		echo "unknown database type: $DB_TYPE" >&2
		exit 2
		;;
esac
bench --site "$SITE" list-apps
echo "::endgroup::"

echo "Running db_smoke.py on $SITE ($DB_TYPE)"
cd sites
set +e
../env/bin/python "$SRC/ci/db_smoke.py" "$SITE" 2>&1 | tee /tmp/db_smoke.log
status=${PIPESTATUS[0]}
set -e
if [ "$status" -ne 0 ] || grep -q '^FAIL' /tmp/db_smoke.log || ! grep -q '^all checks passed' /tmp/db_smoke.log; then
	echo "::error title=db_smoke ($DB_TYPE)::smoke test failed (exit $status)"
	exit 1
fi

# Optional synthetic load (workflow_dispatch input load_test), on the same throwaway site.
if [ "${WOLF_LOAD_TEST:-0}" = 1 ]; then
	echo "Running load_test.py on $SITE ($DB_TYPE) ${WOLF_LOAD_ARGS:-}"
	# shellcheck disable=SC2086 # WOLF_LOAD_ARGS is a list of flags
	../env/bin/python "$SRC/ci/load_test.py" "$SITE" ${WOLF_LOAD_ARGS:-}
fi
