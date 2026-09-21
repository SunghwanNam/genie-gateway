#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "sudo ./install.sh 로 실행하세요." >&2
  exit 1
fi

package_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# /opt/genie-gateway는 ssm-user의 소스/패키지 작업 공간으로 유지하고,
# root가 관리하는 실제 실행 파일은 runtime 하위에 분리한다.
install_root="/opt/genie-gateway/runtime"
release_name="$(basename "${package_dir}")"
release_dir="${install_root}/releases/${release_name}"

if ! id genie-gateway >/dev/null 2>&1; then
  useradd --system --home-dir "${install_root}" --shell /sbin/nologin genie-gateway
fi

mkdir -p "${install_root}/releases" /etc/genie-gateway "${release_dir}"
cp -a "${package_dir}/." "${release_dir}/"
ln -sfn "${release_dir}" "${install_root}/current"

if [[ ! -f /etc/genie-gateway/gateway.env ]]; then
  cp "${package_dir}/gateway.env.example" /etc/genie-gateway/gateway.env
fi

chown -R root:genie-gateway "${install_root}" /etc/genie-gateway
chmod 750 "${install_root}" "${install_root}/releases" "${release_dir}" /etc/genie-gateway
chmod 640 /etc/genie-gateway/gateway.env
install -m 0644 "${package_dir}/genie-gateway.service" /etc/systemd/system/genie-gateway.service
systemctl daemon-reload

echo "설정 파일 수정: /etc/genie-gateway/gateway.env"
echo "DB 스키마 설치 후 실행: systemctl enable --now genie-gateway"
