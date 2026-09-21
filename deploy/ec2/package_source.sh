#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/../.." && pwd)"
version="${1:-0.1.0}"
output_dir="${project_dir}/build/source-package"
archive="${output_dir}/genie-gateway-build-source-${version}.tar.gz"

mkdir -p "${output_dir}"

tar \
  --exclude='__pycache__' \
  --exclude='*.py[cod]' \
  --exclude='.DS_Store' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.git' \
  --exclude='.idea' \
  --exclude='.vscode' \
  --exclude='.cursor' \
  --exclude='.pytest_cache' \
  -C "${project_dir}" \
  -czf "${archive}" \
  app \
  requirements.txt \
  deploy/ec2 \
  sql/installation

echo "Created: ${archive}"
