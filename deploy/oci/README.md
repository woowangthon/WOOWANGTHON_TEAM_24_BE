# 보이는 강의: 기존 OCI 운영 환경의 배포·백업

기존 `boinun.service`와 `/home/opc/BE`를 유지한다. 명세는 [CONTRACT.md](../../examples/CONTRACT.md)가 우선이다.
Python 신규 설치, 가상환경 교체, swap 변경, 서비스 재생성은 이번 준비 범위에 포함하지 않는다.
운영 코드·FE 교체, 패키지 설치, 서비스 재시작, 재부팅은 실행하지 않았다.

## 확인한 운영 환경 (2026-11-12)

| 항목 | 실제 구성 |
| --- | --- |
| 인스턴스 | OCI Osaka, `instance-20261111-0942`, `161.33.22.54` |
| OS | Oracle Linux 9.8, x86_64, 약 945MiB RAM |
| 서비스 | `/etc/systemd/system/boinun.service`, opc, enabled/active, Restart=always |
| 앱 / 가상환경 | `/home/opc/BE`, `/home/opc/BE/.venv`, Python 3.11.13 |
| 실행 | Uvicorn 1개 프로세스, `0.0.0.0:8000`, reload 없음 |
| 환경 파일 | `/home/opc/BE/.env`, 기존 내용·권한 보존 |
| SQLite / 업로드 / 이미지 | `/home/opc/BE/data/app.db`, `data/uploads/`, `data/pages/` |
| FE | `/home/opc/BE/frontend_dist/`, 기존 실제 디렉터리 유지 |
| Swap | `/.swapfile`, `/swapfile` 합계 약 2.9GiB 유지 |
| 비공개 백업 | `/home/opc/boinun-backups/<UTC-시각>-<고유값>/`, root 소유 |

서버 BE는 점검 당시 `main`의 `46fd609`로 원격 main과 일치했다.
[BE #13](https://github.com/woowangthon/WOOWANGTHON_TEAM_24_BE/pull/13)은 병합됐다.
[FE #11](https://github.com/woowangthon/WOOWANGTHON_TEAM_24_FE/pull/11)은 마지막 확인 시 **미병합**이다.
최종 FE 교체는 해당 기능이 병합된 FE main을 확인한 뒤 진행한다. 현재 main의 빌드 검증은 최종 교체와 구분한다.
배포 직전에 PR 상태와 원격 main SHA를 다시 확인한다.

운영 서비스는 이미 단일 worker다. `--workers 1` 명시만을 위해 재시작하지 않는다.
[boinun.service.example](boinun.service.example)은 기존 unit 검토용이며 설치·enable용 파일이 아니다.
이전 신규 서비스용 경로로 이전하거나 두 번째 8000 서비스를 만들지 않는다.

## SSH와 읽기 전용 점검

개인키는 로컬 SSH 클라이언트에 경로로만 전달한다. 개인키나 환경 파일 값을 출력하지 않는다.
서버 ED25519 지문은 OCI Console의 초기 부팅 기록과 대조해 다음 값으로 확인했다.

```text
SHA256:xdppe0fUkeIz6d3uo+GoCHSNtYmJh8Q42hQuEFQgHfg
```

검증한 키만 별도 known_hosts에 등록하고 `StrictHostKeyChecking=yes`로 사용한다.
기존 known_hosts는 보존한다. `$SshKey`, `$KnownHosts`는 실제 확인한 로컬 경로를 사용한다.

```powershell
ssh -i $SshKey -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KnownHosts" opc@161.33.22.54
```

서버에서 실행할 기존 환경 검사:

```bash
cd /home/opc/BE
.venv/bin/python -B --version
.venv/bin/python -B -m pip check
systemctl show boinun.service -p ActiveState -p UnitFileState -p MainPID -p NRestarts
free -h
swapon --show
df -h /home/opc/BE
```

`runtime_check.py`는 cgroup과 8000 리스닝 소켓 소유 PID로 실제 worker를 확인하고,
분석 하위 프로세스·활성 연결·데이터 쓰기 FD를 세어 반환한다. 전체 인수나 환경 변수는 출력하지 않는다.
이번 Python 3.11 검사에서는 pip check, requirements 버전 조건, 실행 의존성 및 app.main import가 통과했다.
app lifespan을 시작하지 않았고 DB 초기화나 외부 AI 호출도 하지 않았다.

11/11 OOM 두 건이 있으나 최근 확인한 24시간에는 추가 기록이 없었다. 최근 dnf-makecache는 성공했다.
이 기록을 이유로 서비스를 중단하거나 swap을 새로 만들지 않는다.

## 실행 중 서비스의 전체 백업

[backup_data.py](backup_data.py)의 기존 중지 상태용 `--data-dir --quiesced` 모드와
새 `--app-dir --live-idle` 모드를 구분한다. 실행 중인 서비스에 `--quiesced`를 사용하지 않는다.
새 모드는 서비스를 중지하거나 쓰기 트랜잭션으로 요청을 차단하지 않는다.

검토한 `backup_data.py`, `runtime_check.py` 두 파일만 SSH stdin 또는 별도 도구 폴더에서 실행한다.
이 준비에서는 SSH stdin으로 실행했으며 서버 운영 저장소의 코드를 교체하지 않았다.
아래 `$TOOLS`는 검토한 두 파일을 둔 별도 도구 폴더다. 운영 저장소에 도구를 덮어쓰지 않는다.

```bash
PY=/home/opc/BE/.venv/bin/python
TOOLS=/tmp/boinun-tools
# 먼저 기존 완료본/partial과 진행 중 작업을 확인한다. 재개 작업에서 중복 생성하지 않는다.
sudo "$PY" -B "$TOOLS/runtime_check.py" --app-dir /home/opc/BE
sudo "$PY" -B "$TOOLS/backup_data.py" \
  --app-dir /home/opc/BE --live-idle \
  --service-file /etc/systemd/system/boinun.service \
  --backup-root /home/opc/boinun-backups
```

동작과 보장 범위:

- 단일 worker·분석 프로세스·연결·쓰기 FD 및 `processing` 문서를 백업 직전에 확인한다. 작업이 있으면 미룬다.
- DB는 read-only 원본 연결에서 SQLite backup API로 생성한다. 실행 중 app.db를 단순 복사하지 않는다.
- 같은 원본 연결의 `PRAGMA data_version`으로 백업 중 외부 커밋 여부를 확인한다.
- uploads, pages, frontend_dist, `.env`, unit 파일의 목록·크기·시각·SHA256을 전후 비교한다.
- 복사 후와 목적지 검증 후에 다시 활동·DB·파일 변경 및 서비스 PID를 확인한다.
- 변경이나 새 작업이 감지되면 `.partial-…`을 남기고 완료 이름으로 바꾸지 않는다. partial은 복구 지점으로 인정하지 않는다.
- 모든 백업 디렉터리는 0700, 파일은 0600이다. `.env`와 unit은 바이너리로 비공개 복사하며 값은 출력하지 않는다.
- 이 모드는 **관측 구간 동안 변경이 없음을 검사한 백업**이다. 전역 파일시스템 트랜잭션이나 쓰기 중지 상태로 표현하지 않는다.

백업 구성:

```text
<UTC-시각>-<고유값>/
  data/app.db
  data/uploads/...
  data/pages/...
  frontend_dist/...
  private/.env
  private/boinun.service
  manifest.json
```

새 백업 위치는 앱/Git 디렉터리 밖이어야 한다. 백업·비밀 파일·실제 FE 빌드는 Git에 추가하지 않는다.
기존 백업을 자동 삭제하거나 같은 이름으로 덮어쓰지 않는다.

완료본 검증(복원하지 않음):

```bash
BACKUP=/home/opc/boinun-backups/20261112T141711Z-147ece42
sudo "$PY" -B "$TOOLS/backup_data.py" --verify "$BACKUP"
```

검증기는 파일 전체 목록·체크섬·제한된 권한과 백업 DB의 `PRAGMA integrity_check`를 검사한다.
이번 완료본은 DB 1개, 업로드 4개, 페이지 파일 12개, FE 파일 8개, 비밀 설정/unit 2개를 검증했다.
서비스 PID 95923과 재시작 횟수 0은 그대로 유지됐다. 운영 경로에 복원하지 않았다.

## 현재 FE의 출처와 다음 빌드 기록

확인한 `frontend_dist.manifest.json`, frontend_dist의 build-info/deployment/manifest 기록에는
배포된 FE의 원본 커밋을 확인할 자료가 없었다. 현재 FE 원본 SHA는 **미확인**이다.
파일 수정 시각, BE 커밋, 별도 FE 체크아웃 HEAD로 출처를 추정하지 않는다.

[build_frontend.mjs](build_frontend.mjs)는 깨끗한 FE main과 origin/main SHA 일치를 확인한다.
빈 envDir로 `.env`를 로드하지 않고 `VITE_USE_MOCK_API=false`, `VITE_API_BASE_URL=''`, base `/`로 빌드한다.
새 출력 폴더만 사용하며 다음 공개 기록을 `build-info.json`과 출력 폴더 옆 manifest에 함께 기록한다.

- `fe_commit`: FE main 전체 SHA
- `built_at_utc`: UTC 빌드 시각
- `index_sha256`: 함께 배포한 index의 SHA256
- mock/API 기준 주소의 공개 빌드 설정

원본 커밋 기록은 index 체크섬과 대조한다. 환경 비밀값은 메타데이터에 넣지 않는다.
기존 사용자 FE 작업 폴더는 보존하고 별도 clean main 복제본과 이미 설치된 의존성을 사용한다.
이번 범위에서는 추가 패키지를 설치하지 않는다.

```powershell
node deploy/oci/build_frontend.mjs 'C:\path\to\clean-FE-main' 'C:\path\to\new-output'
```

## 최종 FE 교체 명령 — 이번에는 실행하지 않음

FE #11 병합 및 main SHA 확인, 빌드·metadata·검증과 최신 백업 확인 후에만 수행한다.
현재 FE main 빌드가 성공해도 미병합 학생 화면의 완료를 의미하지 않는다.

PC에서 내용물을 묶고 전송한다. 서버에서는 stage와 기존 frontend_dist를 분리한다.

```powershell
tar -czf frontend.tar.gz -C 'C:\path\to\new-output' .
Get-FileHash -Algorithm SHA256 frontend.tar.gz
scp -i $SshKey -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KnownHosts" frontend.tar.gz opc@161.33.22.54:/home/opc/frontend.tar.gz
```

서버에서 SHA256·파일 목록·build-info SHA·index 체크섬을 먼저 대조한다.
`FE_SHA`는 실제 main 전체 SHA로 지정한다. 아래 명령은 FE 교체를 승인받은 작업에서만 실행한다.

```bash
set -euo pipefail
: "${FE_SHA:?Set the confirmed FE main SHA}"
[[ "$FE_SHA" =~ ^[0-9a-f]{40}$ ]]
STAGE=/home/opc/fe-staging/$FE_SHA
DIST=/home/opc/BE/frontend_dist
test ! -e "$STAGE"
mkdir -p "$STAGE"
tar -xzf /home/opc/frontend.tar.gz -C "$STAGE" --no-same-owner --no-same-permissions
test -f "$STAGE/index.html" && test -f "$STAGE/build-info.json"
test -d "$DIST" && test ! -L "$DIST"
# 이전 해시 자산은 남겨 기존 브라우저 탭의 요청도 보존한다.
find "$STAGE" -mindepth 1 -maxdepth 1 ! -name index.html ! -name build-info.json \
  -exec cp -a -- {} "$DIST/" \;
install -m 0644 "$STAGE/build-info.json" "$DIST/.build-info.next.json"
install -m 0644 "$STAGE/index.html" "$DIST/.index.next.html"
mv -f "$DIST/.build-info.next.json" "$DIST/build-info.json"
mv -f "$DIST/.index.next.html" "$DIST/index.html"
```

고정 디렉터리에서 자산을 먼저 준비하고 index를 마지막에 원자적 파일 교체한다.
정적 FE 교체만을 위해 boinun.service를 재시작하지 않는다. DB·업로드·페이지·.env·가상환경은 그대로 둔다.
교체 후 GET 전용 `smoke_check.py`, index/metadata 일치, 실제 브라우저 및 학생 조회 흐름을 확인한다.
사용하지 않는 이전 자산 정리는 이번 범위에 포함하지 않는다.

## 복구 절차 — 준비만 수행, 운영 복원 미실행

검증 실패한 partial은 사용하지 않는다. 전체 백업의 `--verify`를 먼저 통과시킨다.
FE만 되돌릴 때는 백업 frontend_dist의 자산을 먼저 복사하고 index를 마지막에 교체하는 같은 순서를 사용한다.
백업 파일은 root 전용이므로 먼저 별도 복구 stage로 복사하고 FE stage만 opc 소유, 디렉터리 0755/파일 0644로 준비한다.
비밀 설정이 있는 private 디렉터리의 권한은 완화하지 않는다.
백업에 build-info가 없다면 새 build-info는 비공개 보존 위치로 옮기고 FE SHA를 미확인으로 표시한다.

DB 복원이 필요한 별도 작업에서는 업로드를 통제하고 processing 0건을 확인한 뒤 writer를 중지한다.
백업 이후 데이터 소실 범위를 확인하고 현재 data도 보존한 후 아래 순서로 복원한다.

```bash
# 아래는 복구 작업용 명령이며 이번 준비에서는 실행하지 않았다.
sudo systemctl stop boinun.service
RECOVERY=$(mktemp -d /home/opc/boinun-restore.XXXXXXXX)
sudo cp -a "$BACKUP/data" "$RECOVERY/data"
sudo chown -R opc:opc "$RECOVERY/data"
# 현재 DB와 WAL/SHM은 폴더 전체를 별도 보존하고 복구본과 섞지 않는다.
sudo mv /home/opc/BE/data "$RECOVERY/preserved-data"
sudo mv "$RECOVERY/data" /home/opc/BE/data
sudo systemctl start boinun.service
```

복구본은 새 별도 디렉터리에서 먼저 검사한다. SQLite backup API로 만든 DB에는 WAL 내용이 반영돼 있다.
운영 중 DB 파일 위에 덮어쓰거나 기존 data를 삭제하지 않는다.
`.env` 또는 unit도 되돌려야 할 때만 현재 파일을 비공개 위치에 먼저 보존하고, 각각 다음처럼 복구한다.

```bash
sudo install -o opc -g opc -m 0600 "$BACKUP/private/.env" /home/opc/BE/.env
sudo install -o root -g root -m 0600 "$BACKUP/private/boinun.service" /etc/systemd/system/boinun.service
sudo systemctl daemon-reload
```

환경/unit 복구도 서비스 중지 상태의 별도 복구 작업이다. 원본 값이나 백업 값을 출력하지 않는다.
기존 `.venv`는 보존하며 Python 설치나 서비스 이름 변경을 복구 절차에 섞지 않는다.

## 네트워크·검증·운영

사용자가 OCI Console에서 확인한 보안 목록은 stateful TCP 22/8000 허용이다. NSG 연결 여부는 미확인이다.
OS firewalld는 `public` zone의 `ens3`에 8000을 runtime/permanent 모두 허용하며 SELinux는 Enforcing이다.
외부 GET 검사에서 health·docs·API 404/422·첫 화면·SPA·JS/CSS·OpenAPI가 통과했다.
이번 작업에서는 OCI 규칙이나 OS 방화벽을 변경하지 않는다.

```bash
python -B deploy/oci/smoke_check.py --base-url http://161.33.22.54:8000
```

실제 브라우저·재부팅·배포된 FE의 원본 커밋은 별도 확인 항목이다.
운영 기간 2026-11-21까지 health·메모리·디스크·백업 상태를 확인하고 데이터와 백업을 보존한다.
자동 종료·백업 삭제·유료 리소스 생성·IP 변경·push·PR 생성·병합은 이 준비에서 하지 않는다.

검증 기록: 로컬 Python 3.14에서 테스트 129개와 Python 소스 18개 문법 검사가 통과했다.
FE main `3d4e6d7`의 lint·빌드 및 임시 Uvicorn/DB에서 metadata와 index 체크섬 일치도 확인했다.
서버의 실제 백업 실행·검증은 기존 Python 3.11.13에서 통과했다. 패키지 설치와 운영 복원은 수행하지 않았다.

참고: [Python 3.11 SQLite backup API](https://docs.python.org/3.11/library/sqlite3.html#sqlite3.Connection.backup),
[같은 연결에서의 SQLite data_version](https://www.sqlite.org/pragma.html#pragma_data_version),
[Vite 환경 변수](https://vite.dev/guide/env-and-mode).
