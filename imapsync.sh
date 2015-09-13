#!/bin/sh

set -e

cd -- "$(dirname "$0")" || exit 1
BASEDIR="$(pwd)"

do_backup_with_config() {
	local LOCALDIR REMOTEUSER REMOTEPASS REMOTESERV
	. "$1" || return 1
	shift
	if [ -z "$LOCALDIR"   ]; then return 1; fi
	if [ -z "$REMOTEUSER" ]; then return 1; fi
	if [ -z "$REMOTEPASS" ]; then return 1; fi

	local ssl_opt=""
	[ "${REMOTESSL:-}" = "yes" ] && ssl_opt="--ssl"
	[ ! -d "$LOCALDIR" ] && mkdir -- "$LOCALDIR"
	(
		cd -- "$LOCALDIR" && \
		python "$BASEDIR/imapbackup.py" --compress=none "$@" $ssl_opt -s "$REMOTESERV" -u "$REMOTEUSER" -p "$REMOTEPASS"
	)
	return $?
}


ALL_OK=true
for c in config.d/*.conf; do
	do_backup_with_config "$c" || ALL_OK=false
done

if $ALL_OK; then
	echo "[OK] all done without error."
else
	echo "[NOT-OK] some error are got!"
fi
$ALL_OK
exit $?
