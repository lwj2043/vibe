# Vibe Coding Service

Ollama 기반 듀얼 모델 코드 생성 서비스입니다.  
`chat_ui.html`을 웹 UI로, FastAPI `server.py`가 백엔드를 담당합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `server.py` | FastAPI 서버. UI 서빙, 로그인, 채팅 API |
| `chat_ui.html` | 채팅 웹 UI |
| `users.json` | 계정 목록 (PBKDF2 해시 저장) |
| `add_user.py` | 계정 추가/비밀번호 변경 CLI |
| `pipelines/dual_model_pipeline.py` | 명세 모델 + 코드 모델 파이프라인 |
| `pipelines/config.json` | Ollama URL, 모델명 설정 |

## 1. 모델 설정

```bash
cp pipelines/config.example.json pipelines/config.json
```

`pipelines/config.json` 예시:

```json
{
  "ollama_url": "http://localhost:11434",
  "plan_model": "llama3.1:8b",
  "dev_model": "qwen2.5-coder:7b"
}
```

- `ollama_url`: Ollama가 실행 중인 주소 (로컬: `http://localhost:11434`, 원격 서버: `http://192.168.x.x:11434`)
- `plan_model`: 명세(spec) 생성 모델
- `dev_model`: 코드 생성 모델

필요한 모델 다운로드:

```bash
ollama pull llama3.1:8b
ollama pull qwen2.5-coder:7b
```

## 2. 실행

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

브라우저에서 `http://localhost:8000` 접속.  
최초 실행 시 `users.json`이 자동 생성되며 기본 계정은 `admin / admin`입니다.

## 3. 계정 관리

계정 정보는 `users.json` 한 파일로 관리합니다. 비밀번호는 PBKDF2-SHA256으로 해시되어 저장됩니다.

**계정 추가 또는 비밀번호 변경:**

```bash
python add_user.py <아이디> <비밀번호>
```

예시:

```bash
python add_user.py admin newpassword   # admin 비밀번호 변경
python add_user.py lee mypassword      # 새 계정 추가
```

`users.json` 형식:

```json
{
  "users": [
    {
      "username": "admin",
      "salt": "<16바이트 hex>",
      "password_hash": "<PBKDF2 SHA-256 hex>"
    }
  ]
}
```

## 4. 연결 오류 해결

`All connection attempts failed` 오류 발생 시:

1. Ollama가 실행 중인지 확인: `ollama serve`
2. `pipelines/config.json`의 `ollama_url`이 올바른지 확인
3. `ollama list`로 설치된 모델 이름 확인 후 `plan_model`/`dev_model`과 일치하는지 확인
4. 원격 서버인 경우 방화벽이 해당 포트(기본 11434)를 허용하는지 확인
