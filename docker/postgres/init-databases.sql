-- 로컬 단일 Postgres 컨테이너 안에서도 운영과 같은 DB 경계를 유지한다.
-- 이 스크립트는 새 volume의 최초 초기화 때만 실행된다.
CREATE DATABASE a360_observability;
CREATE DATABASE a360_rag;
