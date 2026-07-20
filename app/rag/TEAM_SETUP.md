# 팀원용 RAG DB 세팅 가이드

지금 RAG 데이터(문서 임베딩 + 봇/패키지)는 **백엔드 담당자의 로컬 pgvector**에만 있습니다.
팀원(Agent/LLM·프론트·인프라)이 같은 데이터를 쓰려면 아래 중 하나를 택합니다.

---

## 경로 A: seed 덤프 복원 (권장 — 빠르고 무료)

백엔드 담당자가 만든 DB 덤프를 그대로 복원합니다. **임베딩까지 들어있어서 OpenAI 비용 0원, 몇 초면 끝**입니다.

### 백엔드 담당자 (덤프 생성 — 1회)
```bash
# a360-postgres 컨테이너가 떠 있는 상태에서
docker exec a360-postgres pg_dump -U a360_admin -d a360 -t rag_documents -Fc \
  > infra/seed/rag_documents.dump
# 이 파일을 팀에 공유 (Git LFS / S3 / 드라이브 — 수십 MB)
```

### 팀원 (복원)
```bash
git clone <repo> && cd A360-Assistant-Backend
cp .env.example .env            # 검색 질의용 OPENAI_API_KEY만 채우면 됨
docker compose up -d db         # 5432 점유 시 .env에서 DATABASE_PORT=5433

# pgvector 확장 먼저 생성 (덤프에는 확장 정의가 없음 — 이 단계 빠지면 복원 실패)
docker exec a360-postgres psql -U a360_admin -d a360 -c "CREATE EXTENSION IF NOT EXISTS vector;"

docker exec -i a360-postgres pg_restore -U a360_admin -d a360 --clean --if-exists \
  < infra/seed/rag_documents.dump
```
끝. `GET /api/rag/search?q=...` 바로 동작합니다.

> 검색 API를 호출하려면 임베딩 provider 키(질의를 벡터로 바꿔야 하므로)가 **각자** 필요합니다.
> `.env`에 `OPENAI_API_KEY` 한 줄만 넣으면 됩니다. (데이터 재생성은 불필요, 질의 임베딩만.)

---

## 경로 B: 처음부터 재생성 (덤프 없이 직접 수집)

데이터 소스가 전부 공개/재현 가능해서 누구나 밑바닥부터 만들 수 있습니다. 단, 문서 크롤링(~10분)과 임베딩(OpenAI 비용 소액)이 듭니다.

```bash
cp .env.example .env            # OPENAI_API_KEY 채우기 (필수)
pip install -r requirements.txt
docker compose up -d db

# 1) 공식 문서 크롤링 (계정 불필요)
python -m app.rag.pipeline crawl

# 2) 공개 봇/패키지 수집 (계정 불필요, GITHUB_TOKEN 있으면 빠름)
python -m app.rag.pipeline harvest-github

# 3) (선택) 회사 Control Room 봇도 수집하려면 .env에 CR_* 채우고
python -m app.rag.pipeline bots --workspace private

# 4) 병합 → 임베딩 → 적재
python -m app.rag.pipeline build
python -m app.rag.pipeline ingest

# 5) 확인
python -m app.rag.pipeline search "엑셀에서 셀 값 읽는 법"
```

---

## 이후: 공용 DB로 전환 (인프라 담당자)

로컬 DB는 각자 따로라 데이터가 흩어집니다. 실제 운영에선 **공용 pgvector 한 대**로 모읍니다.

1. 인프라 담당자가 관리형 Postgres(pgvector 확장 포함, 예: AWS RDS)를 하나 띄움
2. 모두의 `.env`에서 `DATABASE_HOST/PORT/...`를 그 공용 DB로 지정
3. 백엔드 담당자가 `ingest`를 **한 번만** 실행 → 모두가 같은 데이터를 공유
4. 백엔드 API(`/api/rag/search`)도 같은 `DATABASE_*`를 읽으므로 배포 시 그대로 연결됨

즉 지금 로컬에서 검증한 파이프라인을, 공용 DB로 `DATABASE_HOST`만 바꿔 한 번 돌리면 팀 전체가 공유하는 구조가 됩니다.

---

## 참고: 데이터 아티팩트

`data/ingest/`는 `.gitignore` 대상(용량 큼)이라 저장소에 안 올라갑니다.
팀 공유가 필요하면 **경로 A의 DB 덤프**를 쓰는 게 정석입니다.
(원본 jsonl들 — docs.jsonl / bots.jsonl / packages.json / rag_documents.jsonl — 을 공유하면
임베딩만 각자 다시 돌리는 경로 B의 중간 단계로도 쓸 수 있습니다.)
