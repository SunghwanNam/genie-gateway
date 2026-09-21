#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"
version="${1:-0.1.0}"
source_output_dir="${project_dir}/build/source-package"
deploy_output_dir="${project_dir}/build/deploy-package"
source_zip="${source_output_dir}/genie-api-gateway-source-${version}.zip"
apps_zip="${deploy_output_dir}/databricks_app_gateway-${version}.zip"
checksum_file="${project_dir}/build/SHA256SUMS-${version}.txt"
staging_dir="$(mktemp -d)"

cleanup() {
  rm -rf "${staging_dir}"
}
trap cleanup EXIT

mkdir -p "${source_output_dir}" "${deploy_output_dir}"

"${script_dir}/prepare_databricks_app_gateway.sh"
"${script_dir}/ec2/package_source.sh" "${version}"

mkdir -p "${staging_dir}/genie-api-gateway"
rsync -a \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.venv/' \
  --exclude='.idea/' \
  --exclude='.vscode/' \
  --exclude='.cursor/' \
  --exclude='.databricks/' \
  --exclude='.pytest_cache/' \
  --exclude='__pycache__/' \
  --exclude='*.py[cod]' \
  --exclude='.DS_Store' \
  --exclude='build/' \
  --exclude='outputs/' \
  --exclude='tmp/' \
  "${project_dir}/" "${staging_dir}/genie-api-gateway/"

rm -f "${source_zip}" "${apps_zip}"
(
  cd "${staging_dir}"
  zip -qry "${source_zip}" genie-api-gateway
)
(
  cd "${script_dir}"
  zip -qry "${apps_zip}" databricks_app_gateway \
    -x '*/.databricks/*' '*/__pycache__/*' '*.py[cod]' '*/.DS_Store'
)

cp "${source_zip}" "${source_output_dir}/genie-api-gateway-source.zip"
cp "${apps_zip}" "${deploy_output_dir}/databricks_app_gateway.zip"

(
  cd "${project_dir}"
  shasum -a 256 \
    "build/source-package/genie-api-gateway-source-${version}.zip" \
    "build/source-package/genie-gateway-build-source-${version}.tar.gz" \
    "build/deploy-package/databricks_app_gateway-${version}.zip" \
    > "${checksum_file}"
)

echo "Created release packages for version ${version}:"
echo "  ${source_zip}"
echo "  ${source_output_dir}/genie-gateway-build-source-${version}.tar.gz"
echo "  ${apps_zip}"
echo "  ${checksum_file}"
echo
echo "The EC2 executable tarball must be built on Amazon Linux 2023 with the same CPU architecture as the target EC2 instance."
