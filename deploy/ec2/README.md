# Amazon Linux 2023 배포 절차

## 0. EC2 빌드용 소스 패키지 생성

개발 PC에서는 다음 명령으로 전체 Supervisor API와 설치 SQL이 포함된 빌드용 소스
패키지를 만든다.

```bash
./deploy/package_release.sh <VERSION>
```

`build/source-package/genie-gateway-build-source-<VERSION>.tar.gz`를 대상 EC2와 같은
CPU 아키텍처의 Amazon Linux 2023 빌드 서버로 전달한다. macOS에서 이 소스 패키지는
만들 수 있지만 Linux용 Nuitka 실행 패키지는 만들지 않는다.

## 1. 빌드 서버에서 tar.gz 생성

빌드 서버는 대상 EC2와 동일한 Amazon Linux 2023 및 CPU 아키텍처를 사용한다.

```bash
sudo dnf install -y python3.11 python3.11-devel gcc gcc-c++ patchelf
chmod +x deploy/ec2/build_tarball.sh
./deploy/ec2/build_tarball.sh <VERSION>
```

생성 파일:

```text
build/ec2/genie-gateway-<VERSION>.tar.gz
```

최종 실행 패키지의 `sql/installation/`에는 신규 설치 SQL과 기존 환경 업데이트 SQL이
모두 포함된다.

## 2. EC2에 PostgreSQL 15 설치

```bash
sudo dnf install -y postgresql15 postgresql15-server postgresql15-contrib
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

## 3. DB 및 계정 생성

```bash
sudo -u postgres psql
```

```sql
CREATE ROLE genie_gateway LOGIN PASSWORD 'CHANGE_ME_DB_PASSWORD';
CREATE DATABASE genie_gateway OWNER genie_gateway;
\q
```

`/var/lib/pgsql/data/pg_hba.conf`의 일반 규칙보다 위에 다음 항목을 추가한다.

```text
host    genie_gateway    genie_gateway    127.0.0.1/32    scram-sha-256
```

```bash
sudo systemctl restart postgresql
```

## 4. 패키지 전송 및 설치

```bash
scp build/ec2/genie-gateway-<VERSION>.tar.gz ec2-user@<EC2_HOST>:/tmp/
ssh ec2-user@<EC2_HOST>
sudo mkdir -p /opt/genie-gateway/packages
sudo tar -xzf /tmp/genie-gateway-<VERSION>.tar.gz -C /opt/genie-gateway/packages
cd /opt/genie-gateway/packages/genie-gateway-<VERSION>
sudo ./install.sh
```

`/opt/genie-gateway`는 `ssm-user`의 빌드·패키지 작업 공간으로 유지한다. 실제 실행
파일은 root가 관리하는 `/opt/genie-gateway/runtime/current` 심볼릭 링크를 사용한다.

## 5. 환경설정

```bash
sudo vi /etc/genie-gateway/gateway.env
```

다음 항목을 실제 값으로 변경한다.

```dotenv
DATABASE_URL=postgresql+asyncpg://genie_gateway:<DB_PASSWORD>@127.0.0.1:5432/genie_gateway
DATABRICKS_HOST=https://<WORKSPACE_HOST>
DATABRICKS_CLIENT_ID=<GATEWAY_SP_APPLICATION_ID>
DATABRICKS_CLIENT_SECRET=<GATEWAY_SP_OAUTH_SECRET>
API_KEY_ENCRYPTION_KEY=<URL_SAFE_BASE64_32_BYTE_KEY>
```

## 6. DB 스키마 설치

```bash
PGPASSWORD='<DB_PASSWORD>' psql -h 127.0.0.1 -U genie_gateway -d genie_gateway \
  -f /opt/genie-gateway/runtime/current/sql/installation/001_create_genie_gw_schema.sql
```

## 7. Gateway 시작

```bash
sudo systemctl enable --now genie-gateway
sudo systemctl status genie-gateway
```

## 8. 확인

```bash
curl http://127.0.0.1:38085/health/live
sudo journalctl -u genie-gateway -f
```

## 9. 업데이트

```bash
sudo tar -xzf /tmp/genie-gateway-<VERSION>.tar.gz -C /opt/genie-gateway/packages
cd /opt/genie-gateway/packages/genie-gateway-<VERSION>
sudo systemctl stop genie-gateway

# 이번 관리자 ID 전환이 아직 적용되지 않은 기존 DB에서 한 번만 실행
PGPASSWORD='<DB_PASSWORD>' psql -h 127.0.0.1 -U genie_gateway -d genie_gateway \
  -f sql/installation/008_rename_default_admin.sql

sudo ./install.sh
sudo systemctl restart genie-gateway

curl http://127.0.0.1:38085/health/live
sudo systemctl status genie-gateway --no-pager
```

`008_rename_default_admin.sql`은 관리자 행의 PK를 유지한 채 ID만 `admin`으로 변경하므로
기존 비밀번호·세션·감사 이력이 보존된다. 이미 적용한 환경에서 다시 실행해도 변경 사항은
없지만, `admin`과 `shnam` 행이 동시에 존재하면 충돌 방지를 위해 중단한다.
