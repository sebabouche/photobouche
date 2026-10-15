#!/bin/sh
# Copy prepared making-of material to the booth Pi, which serves it to the slideshow.
#
#   photobouche prepare /Volumes/Shared/making-of media/prepared
#   deploy/push-media.sh media/prepared raspi4-1.local
#
# Adds and updates, never deletes on the Pi (pass --delete as a third argument to mirror).
# New files appear in the slideshow within a second: no restart needed.
set -eu
SRC=${1:?usage: push-media.sh <prepared dir> <booth host> [--delete]}
HOST=${2:?usage: push-media.sh <prepared dir> <booth host> [--delete]}
EXTRA=${3:-}
ROOT=${PHOTOBOUCHE_ROOT:-/srv/photobouche}
USER_AT=${PHOTOBOUCHE_USER:-pi}@$HOST

for sub in making_of clips; do
  [ -d "$SRC/$sub" ] || continue
  # rsync writes each file under a dot-name and renames it when complete, and an
  # interrupted transfer waits in the hidden partial dir: the booth ignores dot-names,
  # so it never serves half a clip. Dot-files in SRC are `prepare` temporaries.
  rsync -av --partial-dir=.rsync-partial --exclude='.*' $EXTRA "$SRC/$sub/" "$USER_AT:$ROOT/$sub/"
done
