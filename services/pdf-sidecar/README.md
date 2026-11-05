# Vibe PDF 사이드카

[Brainrot pdf-service](../external/Open-Chat-Brainrot/pdf-service) 코드를 그대로 컨테이너로 격리한 사이드카. WeasyPrint+한글 폰트는 시스템 라이브러리(libpango/libcairo)를 요구해 Vibe 본체와 동일 프로세스에 두면 Windows 호스트 의존성이 무거워진다 — Docker 컨테이너로 분리.

## 기동

```powershell
# Vibe 내부 토큰 (사이드카 인증용) 설정
$env:VIBE_INTERNAL_TOKEN = "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
$env:VIBE_PDF_ENABLED = "true"

# 사이드카 빌드 + 기동
docker compose -f pdf-sidecar/docker-compose.yml up -d --build

# Vibe 본체도 같은 토큰으로 기동
python server.py
```

## 동작 확인

```powershell
curl.exe -X POST http://127.0.0.1:8102/pdf/from-markdown `
    -H "X-Port-Project-Internal-Token: $env:VIBE_INTERNAL_TOKEN" `
    -H "Content-Type: application/json" `
    -d '{"markdown":"# 안녕하세요\n```python\nprint(1)\n```","filename":"hello"}'
```

응답의 `download_url` 을 브라우저로 열면 한글/코드 하이라이팅이 제대로 적용된 PDF.

## 종료

```powershell
docker compose -f pdf-sidecar/docker-compose.yml down
```
