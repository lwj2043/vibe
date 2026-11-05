# vibe-rebuild

Open-Chat-Brainrot 패턴을 부분 차용한 vibe 재구조화. Suno / Pink Sphinx 제외, Docker 미사용, PowerShell + Python venv 기반.

## 구조

```
vibe-rebuild/
├── orchestrator/          # 포트 8000 — vibe 본체 (FastAPI)
│   ├── server.py
│   ├── routes/            # auth, chat, memory, spec, tools, openai_compat
│   ├── pipelines/         # dual_model_pipeline 외
│   └── tools/registry.json
├── services/
│   ├── pdf-sidecar/       # 포트 8002 — WeasyPrint
│   └── web-sidecar/       # 포트 8004 — readability + markdownify
├── data/                  # chat_logs, specs, user_memory, exports, users.json
├── deprecated/            # chat_ui.html, assets/ (보존만)
├── scripts/               # bootstrap_venvs.ps1, start_openwebui.ps1
├── tests/unit/
└── logs/
```

## 서비스 / 포트

| 서비스 | 포트 | venv |
|---|---|---|
| Open WebUI | 3000 | venv-webui |
| orchestrator | 8000 | venv |
| pdf-sidecar | 8002 | venv-pdf |
| web-sidecar | 8004 | venv-web |

## 시작

```powershell
# 1) 최초 1회: 4개 venv 부트스트랩
.\scripts\bootstrap_venvs.ps1

# 2) 통합 기동 (Open WebUI + orchestrator + 2 sidecar + cloudflared)
.\start.ps1
```

## OpenAI 호환 proxy

orchestrator (8000) 가 `/v1/chat/completions`, `/v1/models` 노출. Open WebUI 가 `vibe-auto`, `vibe-chat`, `vibe-spec`, `vibe-code` 4개 모델로 인식.

## 인증

- 사용자: Open WebUI 자체 로그인
- 서비스 간: `PORT_PROJECT_INTERNAL_TOKEN` 헤더 (`.vibe_token` 단일 소스)

## 설계 문서

`C:\Users\user\.claude\plans\golden-scribbling-goblet.md`
