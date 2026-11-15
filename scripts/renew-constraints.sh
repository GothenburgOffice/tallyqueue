#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Renew constraints.txt: install requirements.txt, without the old pins, in the Python image the
# Dockerfile uses, and record every package that was installed. Needs Docker. Run the checks
# afterwards and commit both files together.
set -eu
cd "$(dirname "$0")/.."
image="$(sed -n 's/^FROM \(python:[^ ]*\) AS dependencies$/\1/p' Dockerfile)"
[ -n "$image" ] || { echo "No Python image found in the Dockerfile" >&2; exit 1; }
header="$(grep '^#' constraints.txt)"
frozen="$(grep -v '^-c ' requirements.txt | docker run --rm -i "$image" sh -c \
  'cat > /tmp/requirements.txt && pip install -q --disable-pip-version-check --root-user-action=ignore -r /tmp/requirements.txt >&2 && pip freeze --disable-pip-version-check')"
[ -n "$frozen" ] || { echo "Nothing was installed; constraints.txt is unchanged" >&2; exit 1; }
printf '%s\n%s\n' "$header" "$frozen" > constraints.txt
echo "constraints.txt renewed from $image: $(printf '%s\n' "$frozen" | wc -l) packages"
