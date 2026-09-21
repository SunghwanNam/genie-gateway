#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
package_dir="${script_dir}/databricks_app_gateway"

mkdir -p "${package_dir}"

# 배포 패키지는 실행에 필요한 소스만 매번 원본에서 동기화한다.
rsync -a --delete --delete-excluded \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '.DS_Store' \
  "${project_dir}/app/" \
  "${package_dir}/app/"

cp "${project_dir}/app.yaml" "${package_dir}/app.yaml"
cp "${project_dir}/requirements.txt" "${package_dir}/requirements.txt"

printf 'Databricks Apps package prepared: %s\n' "${package_dir}"
