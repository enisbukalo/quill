#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_dir=${1:-"${script_dir}/../quill_api/web"}
deploy_root=${QUILL_WEB_DEPLOY_ROOT:-"${HOME}/.local/share/quill/web"}

if [[ ! -f "${source_dir}/index.html" || ! -f "${source_dir}/app.mjs" ]]; then
  echo "error: ${source_dir} is not a Quill web asset directory" >&2
  exit 2
fi

release_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
release_dir="${deploy_root}/releases/${release_id}"
next_link="${deploy_root}/.current-${release_id}"

mkdir -p -- "${release_dir}"
cp -a -- "${source_dir}/." "${release_dir}/"
ln -s -- "${release_dir}" "${next_link}"
mv -Tf -- "${next_link}" "${deploy_root}/current"

echo "${release_dir}"
