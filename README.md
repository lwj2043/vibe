# Vibe Coding

FastAPI 기반 로컬 AI 코딩 어시스턴트. 브라우저 기반 채팅 UI에서 대화하고, 백엔드는 OpenAI 호환 VLM 엔드포인트(단일 모델)로 명세 생성 → 코드 생성 → 시각 검토까지 처리합니다.

## 주요 기능

- 로그인 / 회원가입 (PBKDF2 해시)
- 브라우저 기반 채팅 UI (`chat_ui.html` 단일 파일)
- 사용자별 채팅/설정 저장 + 턴 단위 감사 로그(JSONL)
- **자동 모드 / 확인 후 진행 모드** 2가지 파이프라인
- 사용자 영구 메모리 (`user_memory/`) — 매 요청에 자동 주입
- 명세서(Spec) 저장 및 재열람
- 이미지/PDF 첨부 (PDF는 텍스트 추출 후 컨텍스트로 투입)
- HTML 결과 미리보기 iframe (정적 자산 404 억제 포함)

## 프로젝트 구조

```text
.
├─ server.py                     # FastAPI 엔드포인트
├─ chat_ui.html                  # 단일 HTML 프런트엔드
├─ requirements.txt              # Python 의존성
├─ users.json                    # 계정 (PBKDF2, gitignored)
├─ chat_logs/<user>/             # state.json(복원용) + log.jsonl(감사)
├─ specs/<user>/                 # 저장된 명세서 (latest.json + 날짜별 jsonl)
├─ user_memory/<user>.json       # 사용자 영구 메모리
├─ pipelines/
│  ├─ dual_model_pipeline.py     # 메인 파이프라인 (명세/코드/검토)
│  ├─ openai_client.py           # OpenAI 호환 클라이언트
│  ├─ prompts.py                 # 시스템 프롬프트
│  ├─ router.py                  # 코딩 요청 판정
│  ├─ spec_storage.py            # 명세 영속화
│  ├─ preview_builder.py         # 미리보기 HTML 구성
│  ├─ screenshot.py              # 시각 검토용 스크린샷
│  └─ config.json                # 엔드포인트/모델 설정 (gitignored)
├─ docs/
│  ├─ routing.md                 # 엔드포인트 라우팅 정리
│  └─ project_reference.md       # 프로젝트 상세 레퍼런스
└─ .gitignore
```

## 요구 사항

- Python 3.10+
- OpenAI 호환 API 엔드포인트 (로컬 vLLM / Ollama `/v1` 호환 모드 등)
- 시각 검토 기능을 쓰려면 Playwright 브라우저 설치 필요

## 설치

```bash
python -m venv venv
```

Windows PowerShell:

```powershell
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

macOS / Linux:

```bash
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 설정

`pipelines/config.example.json`을 `pipelines/config.json`으로 복사한 뒤 엔드포인트와 모델을 지정합니다.

```json
{
  "base_url": "http://192.168.100.13:8000/v1",
  "model": "gemma-4-31b-it",
  "api_key": "",
  "max_review_iterations": 0,
  "review_safety_cap": 50,
  "enable_visual_review": true
}
```

- `base_url`: `/v1`까지 포함한 OpenAI 호환 엔드포인트
- `model`: 단일 VLM (명세/코드/시각 검토 전 단계 동일 모델 사용)
- `max_review_iterations` / `review_safety_cap`: 시각 검토 루프 횟수 상한
- `enable_visual_review`: 스크린샷 기반 시각 검토 on/off

## 실행

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

또는:

```bash
python server.py
```

브라우저에서 `http://127.0.0.1:8000` 접속.

## 계정

`users.json`이 없으면 서버 시작 시 기본 계정을 자동 생성합니다.

- ID: `admin`
- PW: `admin`

비밀번호는 PBKDF2-SHA256 (iters=200_000) + 16바이트 솔트로 해시되어 `salt:hash` 형식으로 저장됩니다.

## 파이프라인 동작

### 자동 모드 (`POST /api/chat`)
명세 생성 → 코드 생성 → (옵션) 시각 검토를 하나의 스트리밍 응답으로 처리.

### 확인 후 진행 모드
1. `POST /api/chat/spec` — 코딩 요청이면 JSON 명세 반환, 일반 질문이면 채팅 응답 반환
2. 사용자가 프런트에서 명세 검토/수정
3. `POST /api/chat/code` — 수정된 명세로 코드 생성 (스트리밍)

### 요청 전처리
매 요청에서 `사용자 메모리 블록 → PDF 첨부 텍스트 → 사용자 메시지` 순서로 합쳐진 뒤 모델에 전달됩니다. 이미지는 별도 채널로 투입.

자세한 엔드포인트 스펙은 [docs/routing.md](docs/routing.md) 참고.

## 데이터 저장

- **계정**: `users.json` (PBKDF2 해시)
- **채팅 복원용**: `chat_logs/<user>/state.json` — sanitize된 전체 덮어쓰기
- **채팅 감사 로그**: `chat_logs/<user>/log.jsonl` — append-only, 한 줄당 `{date,time,input,response}`
- **명세서**: `specs/<user>/latest.json` + `YYYYMMDD.jsonl`
- **영구 메모리**: `user_memory/<user>.json` — 최대 50개 항목, mtime 기반 캐시

전부 `.gitignore`되어 있으며 디렉터리 구조는 `.gitkeep`으로만 유지됩니다.

## 주의 사항

- `users.json`, `chat_logs/*`, `specs/*`, `user_memory/*`는 로컬 데이터입니다. 운영 환경에서는 백업과 접근 권한 관리를 별도로 수행하세요.
- 세션은 `server.py`의 `SESSIONS` 딕셔너리(in-memory)에 저장되므로 서버 재시작 시 모든 로그인이 초기화됩니다.
- PDF 텍스트 추출은 `pypdf`가 설치된 경우에만 동작하며, 미설치 시 조용히 건너뜁니다.

## 개발 메모

- UI는 `chat_ui.html` 단일 파일 (미리보기 iframe 포함).
- 백엔드 라우트는 `server.py` 하나에 모여 있음 — 구조 정리는 [docs/routing.md](docs/routing.md).
- 엔드포인트/모델 이슈가 있으면 먼저 `pipelines/config.json`의 `base_url`과 `model`을 확인.
- E2E 테스트 스크립트는 `tests/e2e.py` (Playwright 기반, gitignored).
