#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/../.." && pwd)"
version="${1:-0.1.0}"
build_dir="${project_dir}/build/ec2"
stage_dir="${build_dir}/genie-gateway-${version}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Amazon Linux 2023과 동일한 CPU 아키텍처의 Linux 빌드 서버에서 실행하세요." >&2
  exit 1
fi

python3.11 -m venv "${build_dir}/venv"
"${build_dir}/venv/bin/pip" install --upgrade pip
"${build_dir}/venv/bin/pip" install -r "${project_dir}/requirements.txt" "Nuitka>=2.7,<3"

rm -rf "${stage_dir}"
mkdir -p "${stage_dir}/bin" "${stage_dir}/sql/installation"

cd "${project_dir}"
"${build_dir}/venv/bin/python" -m nuitka \
  --standalone \
  --follow-imports \
  --include-data-dir=app/static=app/static \
  --output-dir="${build_dir}" \
  --output-filename=genie-gateway \
  "${script_dir}/gateway_entrypoint.py"

cp -a "${build_dir}/gateway_entrypoint.dist/." "${stage_dir}/bin/"
cp "${project_dir}/sql/installation/001_create_genie_gw_schema.sql" "${stage_dir}/sql/installation/"
cp "${project_dir}/sql/installation/003_verify_installation.sql" "${stage_dir}/sql/installation/"
cp "${script_dir}/gateway.env.example" "${stage_dir}/"
cp "${script_dir}/genie-gateway.service" "${stage_dir}/"
cp "${script_dir}/install.sh" "${stage_dir}/"
chmod +x "${stage_dir}/install.sh" "${stage_dir}/bin/genie-gateway"

tar -C "${build_dir}" -czf "${build_dir}/genie-gateway-${version}.tar.gz" "genie-gateway-${version}"
echo "Created: ${build_dir}/genie-gateway-${version}.tar.gz"
