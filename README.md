# Vibe Coding

Vibe Coding은 로컬 PC에서 실행하는 브라우저 기반 AI 코딩 워크스페이스입니다. 채팅으로 요청을 입력하면 로컬 Ollama 모델을 호출하고, 생성된 코드는 브라우저에서 바로 읽고 미리 실행해 볼 수 있습니다.

백엔드는 FastAPI로 구성되어 있고, 프론트엔드는 별도 프레임워크 없이 HTML/CSS/JavaScript로 동작합니다.

## 주요 기능

- 로그인, 회원가입, 사용자별 채팅 저장
- AI 응답 스트리밍 표시와 생성 중단
- 명세 생성 후 코드 생성, 또는 바로 코드 생성
- 이미지, PDF, 텍스트, 마크다운 파일 첨부
- 마크다운, 코드 블록, 수식, Mermaid 다이어그램 렌더링
- 코드 복사, 단일 파일 미리보기, 여러 파일 전체 미리보기
- 코드 편집과 iframe 실행을 함께 제공하는 미리보기 패널
- 사용자 기억, 개인 노트
- 브라우저 로컬 저장소와 서버 저장소를 함께 사용하는 상태 복원

프론트엔드 기능을 더 자세히 보려면 [FRONTEND_FEATURES.md](FRONTEND_FEATURES.md)를 참고하세요.

프로젝트 전체 구조는 [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)에 정리되어 있습니다.

현재 코드 리뷰 메모는 [CODE_REVIEW.md](CODE_REVIEW.md)에 정리되어 있습니다.

## 기술 스택

- Python 3.10 이상
- FastAPI
- Uvicorn
- Pydantic
- httpx
- Ollama
- Vanilla HTML/CSS/JavaScript
- highlight.js, KaTeX, Mermaid

## 빠른 시작

먼저 Ollama를 설치하거나 실행한 뒤 기본 모델을 내려받습니다.

```powershell
ollama pull gemma4:e4b
```

가상환경을 만들고 의존성을 설치합니다.

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

서버를 실행합니다.

```powershell
python server.py
```

브라우저에서 접속합니다.

```text
http://127.0.0.1:8000
```

`users.json`이 없으면 서버가 기본 계정을 자동으로 만듭니다.

```text
ID: admin
PW: admin
```

로컬 개발을 넘어 실제로 사용할 경우 기본 계정은 반드시 변경하세요.

## 설정

기본값을 바꾸고 싶다면 `pipelines/config.example.json`을 참고해 `pipelines/config.json`을 만듭니다.

```json
{
  "ollama_url": "http://localhost:11434",
  "model": "gemma4:e4b",
  "plan_model": "gemma4:e4b",
  "dev_model": "gemma4:e4b",
  "coder_model": "gemma4:e4b",
  "keep_alive": -1,
  "max_review_iterations": 1,
  "review_safety_cap": 1,
  "enable_visual_review": false
}
```

환경변수로도 설정을 덮어쓸 수 있습니다.

- `VIBE_OLLAMA_URL` 또는 `OLLAMA_URL`
- `VIBE_OLLAMA_KEEP_ALIVE` 또는 `OLLAMA_KEEP_ALIVE`
- `VIBE_MODEL`
- `VIBE_PLAN_MODEL`
- `VIBE_DEV_MODEL`
- `VIBE_CODER_MODEL`

## 프로젝트 구조

```text
.
├─ server.py                     # FastAPI 앱 진입점
├─ chat_ui.html                  # 브라우저 UI 뼈대
├─ assets/
│  ├─ main.js                    # 프론트 상태, 채팅, 스트리밍, 미리보기 로직
│  ├─ main.css                   # 프론트 스타일
│  └─ preview_core.js            # 미리보기 iframe 보조 로직
├─ routes/
│  ├─ auth.py                    # 인증 API
│  ├─ chat.py                    # 채팅, 명세, 코드 생성 API
│  ├─ memory.py                  # 사용자 기억 API
│  └─ spec.py                    # 저장된 명세 API
├─ pipelines/
│  ├─ dual_model_pipeline.py     # 주요 생성 파이프라인
│  ├─ ollama_client.py           # Ollama API 클라이언트
│  ├─ openai_client.py           # OpenAI 호환 API 보조 클라이언트
│  ├─ prompts.py                 # 프롬프트 템플릿
│  ├─ preview_builder.py         # 미리보기 HTML 구성
│  └─ config.example.json        # 설정 예시
├─ PROJECT_OVERVIEW.md           # 프로젝트 전체 구조 설명
├─ FRONTEND_FEATURES.md          # 프론트 기능 상세 정리
└─ CODE_REVIEW.md                # 코드 리뷰 메모
```

## 로컬 데이터

아래 파일은 실행 중 생성되는 로컬 데이터이며 커밋하지 않는 것이 좋습니다.

- `users.json`: 로컬 사용자 계정
- `chat_logs/<user>/state.json`: 채팅 복원 상태
- `chat_logs/<user>/log.jsonl`: 턴 단위 채팅 로그
- `specs/<user>/latest.json`: 최신 생성 명세
- `specs/<user>/YYYYMMDD.jsonl`: 날짜별 명세 이력
- `user_memory/<user>.json`: 사용자 기억 항목

## 참고 사항

- 기본적으로 로컬 사용을 전제로 합니다.
- 세션은 메모리에 저장되므로 서버를 재시작하면 다시 로그인해야 합니다.
- PDF 텍스트 추출은 `pypdf`가 설치되어 있을 때 동작합니다.
- 생성된 코드는 샌드박스 iframe에서 실행되지만, 신뢰할 수 없는 코드로 취급하는 것이 안전합니다.
- `keep_alive`를 `-1`로 두면 Ollama 모델은 직접 `ollama stop <model>`을 실행하기 전까지 메모리에 남습니다.
