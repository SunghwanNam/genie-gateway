# Databricks Apps 배포 패키지

## 통합 릴리스 패키지 생성

저장소 루트에서 버전을 지정해 다음 명령을 실행하면 EC2 빌드용 소스와 Databricks
Apps 배포 ZIP을 함께 생성한다.

```bash
chmod +x deploy/package_release.sh deploy/ec2/package_source.sh
./deploy/package_release.sh <VERSION>
```

생성 결과는 다음과 같다.

```text
build/source-package/genie-api-gateway-source-<VERSION>.zip
build/source-package/genie-gateway-build-source-<VERSION>.tar.gz
build/deploy-package/databricks_app_gateway-<VERSION>.zip
build/SHA256SUMS-<VERSION>.txt
```

고정 파일명 `genie-api-gateway-source.zip`과 `databricks_app_gateway.zip`도 최신
생성본으로 갱신된다. Secret, `.env`, 가상환경, Git 메타데이터 및 로컬 빌드 결과는
패키지에 포함하지 않는다.

`databricks_app_gateway/`는 Databricks Apps에 업로드할 최소 실행 패키지다.
애플리케이션 소스를 변경한 뒤 다음 명령으로 패키지를 다시 생성한다.

```bash
./deploy/prepare_databricks_app_gateway.sh
```

생성되는 패키지에는 다음 항목만 포함된다.

```text
databricks_app_gateway/
├── app/
├── app.yaml
└── requirements.txt
```

## Databricks App Resource

App 생성 또는 수정 화면에서 다음 Resource를 연결한다.

1. `Database`에서 사용할 Lakebase Autoscaling 프로젝트, 브랜치, 데이터베이스 선택
2. 권한은 `Can connect and create` 선택
3. Resource key는 `postgres` 사용

API Key 원문 재조회 암호화용 Secret Resource도 추가한다.

1. URL-safe base64로 인코딩한 32-byte AES Key를 Databricks Secret에 저장
2. App Resource type `Secret` 추가
3. Resource key는 `api-key-encryption-key` 사용

`app.yaml`의 `LAKEBASE_ENDPOINT: valueFrom: postgres`가 위 Resource의 Endpoint
경로를 주입받는다. 첫 번째 Database Resource의 PostgreSQL 접속 정보는 Databricks
Apps가 `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGSSLMODE`로 자동 주입한다.

Genie Space ID는 Gateway 데이터베이스의 `genie_agt_mst` 및
`sys_genie_agt_map` 라우팅 설정에서 관리하므로 배포 패키지에 하드코딩하지 않는다.
App Service Principal에는 해당 Genie Space와 기반 Unity Catalog 데이터에 필요한
최소 실행/조회 권한을 별도로 부여한다.

## Workspace 업로드 및 배포 예시

```bash
databricks sync \
  ./deploy/databricks_app_gateway \
  /Workspace/Users/<user>/databricks_app_gateway \
  --profile <profile>
```

```bash
databricks apps deploy <app-name> \
  --source-code-path /Workspace/Users/<user>/databricks_app_gateway \
  --mode SNAPSHOT \
  --profile <profile>
```

`.env`, PAT, Service Principal Secret, X-API-Key 원문은 패키지에 포함하지 않는다.

## PrivateLink 및 폐쇄망 의존성

`requirements.txt` 배포는 App 빌드 시 `pypi.org`와 `files.pythonhosted.org`에 접근한다.
PrivateLink 자체만으로 설치가 보장되는 것은 아니며, 제한된 Egress 환경은 두 도메인을
Network Policy에 허용해야 한다.

인터넷 Egress를 완전히 차단하는 운영 환경에서는 macOS에서 받은 wheel을 패키지에
복사하지 않는다. Databricks Apps의 Python 3.11/Linux 환경과 맞는 wheel을 CI에서
준비하여 Unity Catalog Volume에 저장하고, Volume을 App Resource로 연결한 뒤
`requirements.txt`에서 `/Volumes/<catalog>/<schema>/<volume>/...whl` 경로를 참조하거나
사내 Python Package Repository를 `PIP_INDEX_URL`로 설정한다.
