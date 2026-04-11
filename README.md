# Vibe Coding

FastAPI 기반의 로컬 AI 채팅 앱입니다. 브라우저에서 OpenWebUI 스타일 UI로 대화하고, 백엔드에서는 Ollama 모델을 이용한 듀얼 모델 파이프라인으로 응답을 생성합니다.

## 주요 기능

- 로그인/회원가입
- 브라우저 기반 채팅 UI
- 사용자별 채팅 저장
- 채팅 고정, 이름 변경, 내보내기
- 첨부파일 미리보기
- UI 설정
- Ollama 기반 `Spec Model + Coder Model` 파이프라인

## 프로젝트 구조

```text
.
├─ server.py                     # FastAPI 서버
├─ chat_ui.html                  # 단일 HTML 프론트엔드
├─ requirements.txt              # Python 의존성 목록
├─ users.json                    # 사용자 계정 정보
├─ chat_logs/                    # 사용자별 채팅 로그
├─ pipelines/
│  ├─ dual_model_pipeline.py     # Ollama 호출 및 응답 파이프라인
│  └─ config.json                # 모델 / Ollama 주소 설정
└─ .gitignore
```

## 요구 사항

- Python 3.10+
- Ollama
- Ollama에 내려받은 모델
  - 예시: `translategemma:12b`
  - 예시: `qwen2.5-coder:14b`

## 설치

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Ollama 설정

`pipelines/config.json`에서 Ollama 서버 주소와 모델명을 설정합니다.

```json
{
  "ollama_url": "http://host.docker.internal:11434",
  "plan_model": "translategemma:12b",
  "dev_model": "qwen2.5-coder:14b"
}
```

자주 쓰는 로컬 환경 예시:

- 같은 PC에서 Ollama 실행: `http://127.0.0.1:11434`
- Docker/WSL/분리된 환경에서 접근: 네트워크 구조에 맞는 호스트 주소 사용

Ollama 실행 및 모델 준비 예시:

```bash
ollama serve
ollama pull translategemma:12b
ollama pull qwen2.5-coder:14b
```

## 실행

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

또는:

```bash
python server.py
```

브라우저에서 아래 주소로 접속합니다.

```text
http://127.0.0.1:8000
```

## 계정

`users.json`이 없으면 서버가 시작될 때 기본 계정을 자동 생성합니다.

- ID: `admin`
- PW: `admin`

현재 저장된 계정 정보는 `users.json`에 PBKDF2 해시 형태로 저장됩니다.

## 데이터 저장 방식

- 사용자 정보: `users.json`
- 채팅 로그: `chat_logs/{username}.json`
- 저장 시 채팅 원본과 함께 요약 정보(`chat_summaries`, `conversation_logs`)도 같이 기록

## 동작 방식

기본 백엔드는 `Pipeline()` 객체를 통해 사용자 메시지를 Ollama로 전달합니다.

- `single_pass=True`이면 한 번의 모델 호출로 응답 생성
- 아니면 Spec 모델이 JSON 명세를 만들고, Coder 모델이 코드/응답 생성
- `/api/chat`은 결과를 스트리밍으로 브라우저에 전달

## 주의 사항

- `users.json`, `chat_logs/*`는 로컬 데이터이므로 운영 환경에서는 백업과 접근 권한 관리가 필요합니다.
- 세션은 메모리 기반이라 서버 재시작 시 로그인이 초기화됩니다.

## 개발 메모

- UI는 `chat_ui.html` 단일 파일 구조입니다.
- 백엔드는 `server.py` 하나로 라우트(URL 요청)와 저장 로직을 처리합니다.
- Ollama 연결 문제가 있으면 먼저 `pipelines/config.json`의 `ollama_url`과 모델명을 확인하세요.
