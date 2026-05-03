# 프로젝트 개요

이 문서는 Vibe Coding의 전체 구조를 빠르게 파악하기 위한 설명입니다. 처음 보는 사람은 [README.md](README.md)를 먼저 읽고, 프론트 동작은 [FRONTEND_FEATURES.md](FRONTEND_FEATURES.md), 현재 리뷰 메모는 [CODE_REVIEW.md](CODE_REVIEW.md)를 보면 됩니다.

## 한 줄 설명

Vibe Coding은 로컬 Ollama 모델을 이용해 채팅으로 코드를 생성하고, 생성된 프론트엔드 코드를 브라우저에서 바로 미리 실행해 볼 수 있는 로컬 AI 코딩 도구입니다.

## 전체 구성

```text
브라우저 UI
  └─ FastAPI 서버
      ├─ 인증과 채팅 저장
      ├─ 사용자 기억과 명세 저장
      └─ Ollama 모델 호출 파이프라인
```

프론트엔드는 단일 페이지 앱 형태이고, 백엔드는 FastAPI 라우트와 파이프라인 모듈로 나뉩니다. 데이터베이스는 사용하지 않고 로컬 JSON 파일에 상태를 저장합니다.

## 주요 사용자 흐름

1. 사용자가 브라우저에서 로그인합니다.
2. 채팅 목록과 설정을 로컬 저장소와 서버 저장소에서 복원합니다.
3. 사용자가 원하는 앱이나 코드 작업을 자연어로 요청합니다.
4. 서버는 요청이 코딩 작업인지 판단합니다.
5. 필요한 경우 기술 명세 JSON을 생성하고 저장합니다.
6. 코드 생성 단계에서 모델 응답을 스트리밍합니다.
7. 프론트는 응답을 채팅 메시지로 표시하고 코드 블록에 미리보기 버튼을 붙입니다.
8. 사용자는 생성된 HTML, CSS, JS, JSX, TSX 코드를 브라우저 안에서 바로 실행해 볼 수 있습니다.

## 프론트엔드

주요 파일:

- `chat_ui.html`: 화면 구조와 CDN 리소스 선언
- `assets/main.css`: 레이아웃, 테마, 채팅 버블, 코드 블록, 미리보기 패널 스타일
- `assets/main.js`: 상태 관리, 채팅 UI, 스트리밍 처리, 첨부, 설정, 미리보기 로직
- `assets/preview_core.js`: HTML 미리보기 병합과 iframe 샌드박스 보조 로직

프론트는 별도 빌드 과정 없이 브라우저에서 바로 실행됩니다. 응답 렌더링은 자체 마크다운 파서, highlight.js, KaTeX, Mermaid를 조합해 처리합니다.

## 백엔드

주요 파일:

- `server.py`: FastAPI 앱 생성과 정적 파일 서빙
- `routes/auth.py`: 로그인, 회원가입, 로그아웃
- `routes/chat.py`: 채팅 상태 저장, 명세 생성, 코드 생성 스트리밍
- `routes/memory.py`: 사용자 기억 저장과 조회
- `routes/spec.py`: 저장된 기술 명세 조회
- `logging_setup.py`: 로그 설정

세션은 메모리에 저장됩니다. 서버를 재시작하면 로그인 세션은 초기화되지만, 채팅과 사용자 데이터는 로컬 파일에 남습니다.

## 생성 파이프라인

주요 파일:

- `pipelines/dual_model_pipeline.py`: 전체 생성 흐름 제어
- `pipelines/ollama_client.py`: Ollama 네이티브 API 호출
- `pipelines/openai_client.py`: OpenAI 호환 API 호출 보조
- `pipelines/prompts.py`: 명세, 코드 생성, 리뷰 프롬프트
- `pipelines/router.py`: 요청 유형 판단
- `pipelines/spec_storage.py`: 기술 명세 저장
- `pipelines/preview_builder.py`: 코드 블록을 미리보기 HTML로 구성
- `pipelines/screenshot.py`: 시각 검수용 브라우저 렌더링

현재 기본 흐름은 Ollama 모델을 사용합니다. 이미지 입력과 시각 검수 경로는 코드상 남아 있지만, 현재 설정과 완전히 맞지 않는 부분이 있으므로 [CODE_REVIEW.md](CODE_REVIEW.md)를 참고해 정리하는 것이 좋습니다.

## 저장되는 데이터

| 경로 | 내용 |
|---|---|
| `users.json` | 로컬 사용자 계정 |
| `chat_logs/<user>/state.json` | 채팅 복원 상태 |
| `chat_logs/<user>/log.jsonl` | 턴 단위 채팅 로그 |
| `specs/<user>/latest.json` | 최신 생성 명세 |
| `specs/<user>/YYYYMMDD.jsonl` | 날짜별 명세 이력 |
| `user_memory/<user>.json` | 사용자 기억 |
| `logs/` | 실행 로그 |

이 파일들은 로컬 데이터이므로 Git에 올리지 않는 것이 안전합니다.

## 실행에 필요한 것

- Python 3.10 이상
- Ollama
- `gemma4:e4b` 또는 설정 파일에 지정한 Ollama 모델
- Python 패키지 의존성은 `requirements.txt` 참고

기본 실행은 다음 순서입니다.

```powershell
ollama pull gemma4:e4b
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python server.py
```

브라우저에서 `http://127.0.0.1:8000`으로 접속합니다.

## 개발할 때 주의할 점

- 생성된 코드는 iframe 샌드박스에서 실행되지만, 신뢰할 수 없는 코드로 다루는 것이 안전합니다.
- `users.json`, `chat_logs/`, `specs/`, `user_memory/`, `logs/`는 로컬 데이터입니다.
- `pipelines/config.json`은 개인 환경 설정이므로 커밋하지 않습니다.
- 모델을 계속 올려두고 싶으면 `pipelines/config.json`의 `keep_alive`를 `-1`로 둡니다.
- 모델을 수동으로 내릴 때는 `ollama stop <model>`을 사용합니다.
- `.vscode/settings.json`처럼 이미 Git이 추적 중인 파일은 `.gitignore`에 추가해도 자동으로 추적 해제되지 않습니다.
- 서버 코드를 바꾼 뒤에는 실행 중인 서버를 재시작해야 합니다.
