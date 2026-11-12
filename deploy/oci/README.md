# 보이는 강의: OCI 배포 준비

이 문서는 실행 가능한 배포 절차와 로컬 도구를 제공한다. 서버에 적용했다는 기록이 아니다.
명세는 [examples/CONTRACT.md](../../examples/CONTRACT.md)가 우선이다.

## 현재 확인 결과와 배포 조건

2026-11-12 확인:

- BE `main`: `6a9848b`. [BE PR #13](https://github.com/woowangthon/WOOWANGTHON_TEAM_24_BE/pull/13)은 **미병합**이다. `/api/health`와 FE 제공은 `d4ab6ff`에만 있다.
- [FE PR #10](https://github.com/woowangthon/WOOWANGTHON_TEAM_24_FE/pull/10)은 병합됐다. FE `main`은 `3d4e6d7`이다.
- [FE 학생 화면 PR #11](https://github.com/woowangthon/WOOWANGTHON_TEAM_24_FE/pull/11)은 **미병합**이다. 현재 FE main 빌드는 최종 기능 완료본이 아니다.
- SSH는 `161.33.22.54:22`의 호스트 키 검증 단계까지 도달했다. 신뢰할 호스트 키와 개인키 경로가 확인되지 않아 로그인하지 않았다. 인스턴스 정체·OS·Python·8000 포트·파일·서비스 상태는 미확인이다.

`feat/deploy-oci`는 `d4ab6ff`에서 분리한 후속 브랜치다. PR #13 브랜치에는 배포 커밋을 넣지 않는다.
배포 전 PR #13과 필요한 FE PR의 **merged 상태**, 해당 커밋의 각 `origin/main` 포함 여부를 다시 확인한다.
최종 서버에는 **BE main과 FE main**을 사용하고, 배포 도구·서비스 파일도 검토·병합된 버전을 사용한다.
이번 준비에서 PR 생성·push·승인·병합은 하지 않는다.

과거 서버 정보: Osaka `ap-osaka-1`, `instance-20261111-0942`, `VM.Standard.E2.1.Micro`, 1GB,
Oracle Linux 9, 사용자 `opc`, 공인 IP `161.33.22.54`(과거 Ephemeral).
OCI Console에서 현재 인스턴스·공인 IP·할당 유형을 재확인한다. IP 교체·예약·인스턴스 재생성은 하지 않는다.
목표 주소는 `http://161.33.22.54:8000`, 운영 기간은 2026-11-21까지다.

## SSH와 서버의 읽기 전용 점검

개인키는 사용자가 지정한 **경로만** 사용한다. 파일 내용과 `.env`를 열거나 출력하지 않는다.
호스트 키의 SHA256 지문은 OCI Console/기존 신뢰 기록으로 대조한다. `StrictHostKeyChecking=no`로 우회하지 않는다.
`ssh-keyscan` 결과만으로 서버의 신원을 확정하지 않는다. 대조 후에만 호스트 공개키를 known_hosts에 등록한다.
신뢰할 수 있는 인스턴스 콘솔에서는 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256`으로
서버 **공개** 호스트 키의 지문을 확인할 수 있다. SSH 접속용 개인키의 지문과 혼동하지 않는다.

Windows PowerShell의 접속 예시(아래 키 경로는 실제 경로로 바꿔야 한다):

```powershell
$SshKey = 'C:\path\to\the-confirmed-private-key'
ssh -i $SshKey -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes opc@161.33.22.54
```

인증 후 먼저 다음을 확인한다. 아직 설치·재시작·방화벽 변경은 하지 않는다.

```bash
id
cat /etc/os-release
uname -m
free -h
df -h / /opt /var
python3 --version
command -v python3.12 && python3.12 --version
dnf list --available python3.12 python3.12-pip
sudo -n ss -ltnp '( sport = :22 or sport = :8000 )'
systemctl list-unit-files --type=service | grep -Ei 'visible|lecture|uvicorn|fastapi' || true
systemctl show visible-lecture.service -p FragmentPath -p ActiveState -p UnitFileState -p User -p WorkingDirectory
sudo -n firewall-cmd --state
sudo -n firewall-cmd --get-active-zones
getenforce
```

기존 앱의 위치가 확인되면 코드 커밋, data 디렉터리의 실제 경로, 여유 공간, 소유자,
실행 중인 서비스를 기록한다. 전체 실행 인수·프로세스 환경·`.env`·개인키는 출력하지 않는다.
서비스가 이미 있으면 이름·실행 사용자·경로를 이 문서와 대조하고 유지한다. 아래 기본 경로로 덮어쓰지 않는다.
SSH만으로 OCI 보안 목록이나 Ephemeral 여부를 확인했다고 간주하지 않는다.

## 경로와 실행 구성

다음은 **신규 설치용 제안 경로**다. 기존 배포가 발견되면 데이터 이전·백업을 먼저 계획한다.

| 항목 | 경로 |
| --- | --- |
| BE 릴리스 | `/opt/visible-lecture/releases/<BE-main-SHA>` |
| 실행 링크 | `/opt/visible-lecture/current` → 해당 릴리스 |
| FE 정적 파일 | 각 릴리스의 `frontend_dist/index.html`, `frontend_dist/assets/…` |
| 가상환경 | 각 릴리스의 `.venv` (서버 Python 3.12로 생성) |
| 영속 데이터 | `/var/lib/visible-lecture/data` |
| 릴리스의 데이터 링크 | `<release>/data` → `/var/lib/visible-lecture/data` |
| SQLite / PDF / PNG | `data/app.db`, `data/uploads/`, `data/pages/` |
| 비밀 환경 파일 | `/etc/visible-lecture.env` (root:root, 0600) |
| 백업 | `/var/backups/visible-lecture/<UTC-시각>-<고유값>/` (공개 경로 밖) |
| 서비스 | `/etc/systemd/system/visible-lecture.service` |

`app/db.py`의 DB 경로는 소스 파일 기준으로 고정돼 있다. `DATABASE_URL` 같은 미지원 설정을 추가하지 않는다.
각 릴리스의 `data` 링크가 영속 데이터를 가리켜야 한다. `pdf_processing.py`도 릴리스 디렉터리를
작업 경로로 사용하므로 이 구조에서 PDF 하위 프로세스가 같은 코드·가상환경·데이터를 사용한다.
AI는 Python 프로세스의 환경을 사용한다. systemd의 EnvironmentFile이 하위 프로세스에도 전달한다.

기존 배포·데이터 경로와 충돌하지 않는 신규 설치에서만 아래 부모 디렉터리를 준비한다.
실제 릴리스 디렉터리는 확인한 SHA 이름으로 따로 생성하고 `opc` 소유로 둔다.

```bash
sudo install -d -o opc -g opc -m 0700 /opt/visible-lecture /opt/visible-lecture/releases
sudo install -d -o opc -g opc -m 0700 /var/lib/visible-lecture /var/backups/visible-lecture
```

기존 데이터가 전혀 없는 최초 설치에는 `/var/lib/visible-lecture/data`도 `opc:opc`, 0700으로 만든다.
데이터가 발견됐다면 새 빈 DB로 대신하지 말고 백업·이전 절차를 먼저 수행한다.

Oracle Linux 9.4 이상에서는 AppStream의 Python 3.12를 우선 검토한다.
서버의 실제 minor 버전과 패키지 가용성을 확인한 뒤 다음을 실행한다. OS 기본 Python을 교체하지 않는다.

```bash
sudo dnf install python3.12 python3.12-pip git
python3.12 --version
python3.12 -m venv /opt/visible-lecture/releases/<BE-main-SHA>/.venv
cd /opt/visible-lecture/releases/<BE-main-SHA>
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -B -c 'import app.main; print("app import OK")'
```

`<BE-main-SHA>`는 실제 확인한 main SHA로 치환한다. 먼저 릴리스 디렉터리를 `opc` 소유로 준비한다.
의존성 설치 결과를 기록하고 같은 릴리스의 가상환경을 보존해 롤백에 사용한다. PC의 Windows 가상환경은 전송하지 않는다.
1GB 서버에서 FE 빌드나 패키지 소스 컴파일을 강행하지 않는다. wheel이 없거나 메모리가 부족하면 설치를 멈추고 원인을 확인한다.
Python 3.12가 없다면 임의로 OS 업그레이드하지 말고 별도로 판단한다. 로컬 검증은 Python 3.14.3에서 수행했으므로 3.12 서버 검증은 별도다.

수동 실행 명령(설치 점검용):

```bash
cd /opt/visible-lecture/current
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

운영에는 [visible-lecture.service](visible-lecture.service)를 사용한다. `--reload`는 사용하지 않는다.
`opc`로 실행하며 자동 재시작·부팅 시 시작을 지원한다. `KillMode=control-group`은 중지 시 PDF 하위 프로세스도 정리한다.
현재 백그라운드 작업은 영속 작업 큐가 아니므로, **processing 문서가 없는 상태에서** 중지·재배포한다.
대기 중인 문서가 있으면 완료를 기다리고 중지 중 새 업로드가 들어오지 않도록 접근을 통제한다.

```bash
if ! sudo test -e /etc/visible-lecture.env; then
    sudo install -o root -g root -m 0600 deploy/oci/environment.example /etc/visible-lecture.env
fi
sudoedit /etc/visible-lecture.env
```

위 환경 파일 설치는 **처음 만들 때만** 실행한다. 기존 파일은 덮어쓰지 않는다.
실제 Gemini 키는 서버 편집기에서만 입력하고, `echo KEY=…`·`set -x`·환경 덤프는 사용하지 않는다.
`AI_MODE=live`를 사용할 때 키 누락 여부만 확인하고 값은 출력하지 않는다. 로컬 검증에서는 AI 클라이언트를 차단한다.

최초 설치에는 영속 데이터·releases·백업 디렉터리를 `opc:opc`, 0700으로 준비한다.
기존 data가 있으면 아래 백업을 완료한 후 복사본으로 영속 경로를 구성하고 원본을 보존한다.
각 새 릴리스에 `data` 링크를 만들기 전에 그 경로가 비어 있고 다른 실제 데이터가 없는지 확인한다.

```bash
ln -s /var/lib/visible-lecture/data /opt/visible-lecture/releases/<BE-main-SHA>/data
readlink -f /opt/visible-lecture/releases/<BE-main-SHA>/data
```

서비스 파일은 최초 설치 또는 변경분 검토 후에만 설치한다. 기존 unit 파일은 먼저 별도 백업한다.

```bash
sudo install -o root -g root -m 0644 deploy/oci/visible-lecture.service /etc/systemd/system/visible-lecture.service
sudo systemd-analyze verify /etc/systemd/system/visible-lecture.service
sudo systemctl daemon-reload
sudo systemctl enable --now visible-lecture.service
systemctl is-enabled visible-lecture.service
systemctl is-active visible-lecture.service
```

SELinux를 끄지 않는다. 접근 거부가 발생하면 대상 경로·레이블·서비스 제한을 진단한다.
`ProtectSystem=strict`에서 쓰기는 영속 data에만 허용한다. FE·코드·가상환경은 실행 시 읽기 전용이다.

## FE 빌드와 전달 (PC에서만 빌드)

기존 FE 작업 폴더의 수정은 보존한다. 필요한 FE PR 병합 확인 후 별도 빈 디렉터리에 최신 main을 복제한다.
그 복제본에서 `npm ci --ignore-scripts --no-audit --no-fund`를 실행한다.
Node 버전은 FE lockfile·Vite 요구사항과 맞춘다. 이번 로컬 도구는 Node 24.16.0에서 확인했다.

BE 폴더에서:

```powershell
node deploy/oci/build_frontend.mjs 'C:\path\to\clean-FE-main' 'C:\path\to\new\frontend_dist'
```

빌드 도구는 clean main과 origin/main의 SHA 일치를 확인한다. Vite의 envDir을 빈 임시 폴더로 지정해
체크아웃의 `.env`를 로드하지 않는다. `VITE_USE_MOCK_API=false`, `VITE_API_BASE_URL=''`, `base='/'`를
빌드 시 지정한다. 기존 출력 폴더를 덮어쓰지 않고 옆에 FE SHA와 공개 설정만 담은 manifest를 생성한다.
Vite 설정이 바뀌면 이 격리 빌드가 새 설정과 호환되는지 재검증한다. 서버 .env에 VITE 값을 넣어도 이미 생성한 JS는 바뀌지 않는다.

`frontend_dist` **내용물**이 릴리스의 `frontend_dist` 바로 아래 오도록 묶는다. `dist/dist`로 중첩하지 않는다.
Windows의 tar로 공백 경로도 인수 단위로 처리한다:

```powershell
tar -czf 'C:\path\to\frontend.tar.gz' -C 'C:\path\to\new\frontend_dist' .
Get-FileHash -Algorithm SHA256 'C:\path\to\frontend.tar.gz'
scp -i $SshKey -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes 'C:\path\to\frontend.tar.gz' opc@161.33.22.54:/home/opc/frontend.tar.gz
```

서버에서 SHA256을 대조하고 파일 목록이 `index.html`, `assets/…` 등 빌드 파일뿐인지 확인한다.
신규 릴리스의 빈 `frontend_dist`에 압축을 해제한다. 가동 중인 frontend_dist에 덮어쓰지 않는다.
BE 소스는 main의 확인한 SHA를 새로운 release에 체크아웃한다. 비밀 파일·PC data·개인키·node_modules·Windows .venv는 전송하지 않는다.

## 배포 전 백업과 릴리스 전환

1. 코드 SHA, FE SHA, 현재 `current` 링크 대상, unit 파일 경로를 기록한다. 기존 unit은 비공개 백업 위치에 복사한다.
2. 업로드 접근을 통제하고 processing 건수가 0인지 **읽기 전용 SQLite 조회로** 확인한다. 작업이 남으면 기다린다.
3. 기존 서비스 및 다른 수동 실행 프로세스를 중지한다. 모든 writer와 PDF 하위 프로세스가 종료됐는지 확인한다.
4. 아래 도구로 기존 data 경로를 백업한다. `--quiesced`는 서비스·writer를 멈췄다는 운영자의 확인이다. 도구 자체가 서비스를 중지하지 않는다.

```bash
python3.12 deploy/oci/backup_data.py --data-dir /var/lib/visible-lecture/data --backup-root /var/backups/visible-lecture --quiesced
python3.12 deploy/oci/backup_data.py --verify /var/backups/visible-lecture/<완료된-백업>
```

기존 앱의 data가 다른 경로라면 그 경로를 명시한다. 최초 설치에 DB가 없으면 백업을 생략하고 없었다고 기록한다.
도구는 DB를 read-only로 열고 SQLite backup API로 일관된 DB를 생성한다. 실행 중 `cp app.db`를 하지 않는다.
PDF와 PNG까지 같은 시점으로 묶기 위해 모든 writer를 멈춘 상태를 요구한다. 처리 중 문서가 있으면 거부한다.
`uploads`, `pages`를 복사하고 DB integrity 검사·각 파일의 SHA256 manifest를 만든다.
완료 전에는 `.partial-…` 이름을 사용한다. partial 폴더를 복구본으로 사용하지 않는다. 기존 백업을 덮어쓰거나 자동 삭제하지 않는다.
data와 백업 루트의 포함 관계·공간을 확인한다. 백업을 frontend_dist나 공개 디렉터리에 두지 않는다.

5. 신규 릴리스에서 의존성·data 링크·index.html·환경 파일 존재 및 소유자를 확인한다.
6. **서비스 중지 상태에서만** current 링크를 신규 릴리스로 전환한다. 이전 릴리스와 가상환경은 보존한다.
   첫 설치는 `ln -s <release> /opt/visible-lecture/current`, 이후에는 임시 링크를 만든 뒤 `mv -Tf`로 링크만 전환한다.
   current가 실제 디렉터리이면 이 명령을 사용하지 말고 기존 구성을 보존한다.
7. 서비스를 시작하고 아래 내부·외부 HTTP 검사를 모두 수행한다. 신규 업로드는 검사 후 허용한다.

## 8000 포트: OCI와 OS를 각각 확인

OCI Console에서 Osaka → 해당 인스턴스 → VNIC → 서브넷의 보안 목록과 연결 NSG를 확인한다.
기존 TCP 22 규칙을 보존한다. 공개 서비스가 필요하면 적용되는 목록/NSG에 stateful ingress,
TCP, source `0.0.0.0/0`, source port 전체, destination port `8000` 규칙이 있는지 확인하고 없을 때만 추가한다.
인터넷 게이트웨이·공용 서브넷 라우트·현재 공인 IP도 대조한다. AI 호출용 HTTPS egress도 확인한다.

서버에서는 실제 NIC의 활성 zone을 확인한다. `public`이라고 가정하지 않는다.

```bash
sudo firewall-cmd --get-active-zones
sudo firewall-cmd --zone=<확인한-zone> --list-all
sudo firewall-cmd --zone=<확인한-zone> --query-port=8000/tcp
sudo firewall-cmd --permanent --zone=<확인한-zone> --query-port=8000/tcp
```

필요한 경우에만 해당 zone에 runtime과 permanent를 각각 추가한다. 기존 전체 규칙을 교체하지 않는다.

```bash
sudo firewall-cmd --zone=<확인한-zone> --add-port=8000/tcp
sudo firewall-cmd --permanent --zone=<확인한-zone> --add-port=8000/tcp
```

불필요한 `firewall-cmd --reload`를 피한다. firewalld가 inactive면 임의로 활성화하지 말고 실제 nftables 정책을 확인한다.
서버 내부 HTTP만 성공하면 외부 공개 성공으로 기록하지 않는다.

## HTTP와 운영 검증

```bash
python3.12 deploy/oci/smoke_check.py --base-url http://127.0.0.1:8000
```

PC에서는 `.venv/Scripts/python.exe deploy/oci/smoke_check.py --base-url http://161.33.22.54:8000`으로 외부 접근도 확인한다.
검사는 GET만 사용하며 PDF 업로드·AI 호출·DB 쓰기를 하지 않는다.

- `/api/health`: 200 `{"status":"ok"}`.
- `/docs`: HTML 200. `/api`, `/api/`, `/api/not-existing`: 지정한 한국어 JSON 404.
- `/api/documents/not-an-integer`: 기존 validation JSON 422.
- `/`, `/dashboard`, `/review/8`: 같은 index HTML 200.
- index가 참조하는 동일 주소의 JS/CSS: 정상 MIME과 파일 본문. HTML이 대신 오면 실패.
- `/openapi.json`: health 포함, fallback 제외.

빌드가 없을 때의 404·경로 이탈 차단·기존 승인/수정/공개/학생 조회는 `tests/test_frontend_serving.py`에서
임시 DB로 검증한다. 실제 배포 후에는 브라우저에서 첫 화면, 직접 접근/새로고침, 개발자도구의
`/api` 요청 주소·콘솔 오류·mock 미사용을 확인한다. 학생 화면은 관련 PR 병합 후 확인한다.
실제 AI 정확도 검증은 별도 명시적 작업이며 이 smoke test에 포함하지 않는다.

SSH를 종료한 뒤 PC에서 health를 다시 확인한다. `systemctl is-enabled`로 자동 시작 설정을 확인한다.
실제 재부팅 검증은 processing 0건·백업·현재 IP 확인 후 서비스 점검 시간에 수행하고, 실행하지 않았다면 미검증으로 기록한다.

## 실패 시 복구와 11/21까지 운영

화면/정적 파일/health 중 하나라도 실패하면 신규 업로드를 열지 않는다. 서비스를 중지하고 이전 current 링크와
이전 unit(변경했을 때)을 복원한다. `daemon-reload` 후 시작하고 smoke check를 다시 수행한다.
이번 변경에는 DB 스키마 변경이 없으므로 **코드 롤백만으로 되면 최신 data를 유지**한다.

데이터 복원이 필요한 경우:

1. 신규 쓰기를 막고 모든 서비스를 중지한다. 현재 data도 별도 백업/보존한다. 백업 이후 데이터가 있다면 소실 범위를 확인하고 별도 승인을 받는다.
2. `backup_data.py --verify <backup>`로 파일 체크섬과 SQLite integrity를 확인한다.
3. 같은 파일시스템의 새 `data.restore-<고유값>` 디렉터리에 백업의 `app.db`, `uploads`, `pages`를 복사한다.
   백업 DB에는 WAL 내용이 반영돼 있다. 실행 중이던 DB의 `-wal`/`-shm` 파일을 복구본에 섞지 않는다.
4. 기존 data 디렉터리를 보존용 이름으로 옮기고 검증한 복구 디렉터리를 data로 전환한다. 기존 data와 합쳐 덮어쓰거나 삭제하지 않는다.
5. `opc` 소유권·읽기/쓰기 권한·data 링크를 확인한 뒤 이전 코드로 시작한다. health와 화면, 학생 목록을 확인하고 쓰기를 재개한다.

운영 중 매일 health, 서비스 상태, 메모리·디스크 여유, 백업 완료 여부를 확인한다.
일일 전체 백업은 업로드를 잠시 통제하고 작업 완료 후 위 절차로 수행한다. 운영 중 DB 단독 백업을
수행할 때도 SQLite backup API를 사용하며, PDF/PNG까지 동일 시점이라는 보장은 별도로 하지 않는다.
2026-11-21까지 서비스와 백업을 보존하고, 심사 종료 후 중지 여부는 사용자와 정한다.
자동 종료·백업 삭제·유료 리소스 생성·IP 변경은 이 준비에 포함하지 않는다.

## 이번 로컬 검증 기록 (2026-11-12)

- BE Python 3.14.3 가상환경: `pip check` 통과, Python 소스 16개 문법 검사, pytest **118개 통과**.
- 설치된 주요 의존성 7개의 Requires-Python 메타데이터는 3.12를 허용했다. 실제 Python 3.12 설치·실행 검증을 대신하지 않는다.
- 테스트에는 임시 DB·PDF·페이지 이미지만 사용했다. Gemini 클라이언트 생성과 외부 네트워크 연결을 차단했다.
- WAL에만 존재하는 커밋도 백업에 포함됐고, PDF/PNG 복사본·SHA256·변조 및 경로 이탈 탐지를 검증했다.
- FE `main` `3d4e6d7`: Node 24.16.0 / npm 11.13.0, lockfile 설치·lint·production 빌드 성공.
- 컴파일한 FE API 모듈에서 mock 꺼짐, `/api/documents`와 `/api/pages/8/image` 상대 주소를 가짜 fetch로 확인했다.
- 실제 FE 빌드를 임시 Uvicorn+SQLite 서버에서 제공해 smoke check 전체 통과. 임시 테스트 데이터와 서버는 정리했다.
- 실행 중 의존성의 TestClient 사용 중단 예정 경고 1건이 있었으나 테스트 실패는 없었다. 패키지는 업그레이드하지 않았다.
- 실제 브라우저·OCI 내부 상태·Python 3.12·systemd 실행/재부팅·외부 8000 접근은 아직 검증하지 않았다.
- 빌드 아카이브는 PC의 별도 임시 준비 폴더에 보관한다. PR 미병합으로 최종 배포본이 아니며 main 병합 후 재생성한다.

## 공식 참고 문서

- [Oracle Linux 9 Python 설치](https://docs.oracle.com/en/operating-systems/oracle-linux/9/python/python-InstallingPython.html)
- [Oracle Linux Python 패키지](https://yum.oracle.com/oracle-linux-python.html)
- [OCI 네트워크 보안 규칙](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm)
- [Oracle Linux 9 firewalld zone/port](https://docs.oracle.com/en/operating-systems/oracle-linux/9/firewall/firewall-ConfiguringfirewalldZones.html)
- [Python 3.12 SQLite backup API](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.backup)
- [systemd.service](https://www.freedesktop.org/software/systemd/man/252/systemd.service.html)
- [Vite 빌드 환경 변수](https://vite.dev/guide/env-and-mode)
