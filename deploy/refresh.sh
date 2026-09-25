#!/bin/sh
# The scheduled refresh: fetch what changed, then check the result.
#
# A script rather than a `/bin/sh -c` one-liner in the plist so that macOS has
# something to call it. Background Task Management lists a launchd agent by its
# first program argument, so the one-liner appeared in Login Items as "sh" from
# "Unknown Developer" - exactly what gets switched off when tidying that list,
# and a disallowed agent is neither loaded at login nor kept loaded.
#
# Validate only when the refresh succeeded: there is nothing useful to check
# after a fetch that never reached the portal.

set -eu
cd "$(dirname "$0")/.."
.venv/bin/rail refresh
exec .venv/bin/rail validate
