# Psycopg 라이선스 제한 승인 기록

## 결정

RPA-241을 근거로 아래의 수정하지 않은 upstream 배포본에 한해 `LGPL-3.0-only` 사용을 승인한다.

| 배포 패키지 | 고정 버전 | 사용 목적 |
|---|---:|---|
| `psycopg` | `3.2.3` | PostgreSQL 드라이버 |
| `psycopg-binary` | `3.2.3` | Psycopg 바이너리 구현 |
| `psycopg-pool` | `3.3.1` | 동기·비동기 연결 풀 |

이 승인은 LGPL 전체에 대한 전역 허용이 아니다. 패키지명, 버전 또는 라이선스 표현이 달라지면 Change Assurance가
예외를 적용하지 않으며 새 검토가 필요하다. 전역 라이선스·취약점 정책은 계속 `decision_needed` 상태로 유지한다.

## 사용 및 배포 조건

1. upstream 패키지 소스를 수정하거나 프로젝트 코드에 복사하지 않는다.
2. wheel에 포함된 `LICENSE.txt`를 제거하지 않는다.
3. 배포 패키지와 컨테이너에서 upstream 라이선스와 소스 획득 경로를 확인 가능하게 유지한다.
4. 버전 변경, 배포 방식 변경 또는 패키지 수정 시 RPA-241 승인을 재사용하지 않는다.

## 근거

- 결정 추적: [Jira RPA-241](https://metanetfinal.atlassian.net/browse/RPA-241)
- upstream source: [psycopg/psycopg](https://github.com/psycopg/psycopg)
- upstream installation guide: [Psycopg installation](https://www.psycopg.org/psycopg3/docs/basic/install.html)
- license text: 각 설치 배포본의 `.dist-info/LICENSE.txt`

이 문서는 프로젝트의 기술적 사용 범위와 하네스 판정 근거를 기록한다. 일반적인 법률 자문이나 다른 LGPL 패키지의
자동 승인을 의미하지 않는다.
