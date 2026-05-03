# 코드 리뷰 메모

리뷰 날짜: 2026-05-01

검토 범위: 현재 작업 트리의 프론트 스트리밍 변경, 미리보기 샌드박스 변경, Ollama 파이프라인 전환, 채팅 라우트, README, `.gitignore`.

## 주요 발견사항

### P1: 검수와 정리 전에 코드가 먼저 스트리밍됨

파일: `pipelines/dual_model_pipeline.py`

코드 생성 루프가 모델 토큰을 받는 즉시 사용자에게 내보내고 있습니다. 그런데 `<think>` 제거와 자동 검수는 그 뒤에 실행됩니다. 이 구조에서는 생각 과정 텍스트가 UI에 노출될 수 있고, 첫 번째 생성물이 검수에 실패해도 실패한 코드가 이미 화면에 섞일 수 있습니다.

수정 방향: 생성 시도별로 먼저 내부 버퍼에 모은 뒤, 정리와 검수를 통과한 최종 결과만 사용자에게 스트리밍하는 방식이 안전합니다.

### P1: Ollama 경로에서 이미지 첨부가 무시됨

파일: `pipelines/dual_model_pipeline.py`

라우트는 이미지 첨부를 파싱해 파이프라인으로 넘기지만, 현재 `_call()`은 이미지를 모델에 보내지 않고 경고 로그만 남깁니다. 사용자는 이미지를 첨부할 수 있지만, 모델은 이미지를 보지 못한 채 답변하게 됩니다.

수정 방향: Ollama의 이미지 입력을 지원하는 모델과 API 형식을 구현하거나, 비전 모델용 경로를 별도로 두는 것이 좋습니다. 당장 지원하지 않을 거라면 UI와 API 응답에서 이미지 이해가 불가능하다는 점을 분명히 보여줘야 합니다.

### P2: 시각 검수 경로가 Ollama 기본 설정과 맞지 않음

파일: `pipelines/dual_model_pipeline.py`

시각 검수는 아직 `openai_client.call_llm_with_image()`를 사용합니다. 하지만 기본 `base_url`은 Ollama 네이티브 API 주소입니다. `enable_visual_review`를 켜도 잘못된 엔드포인트를 호출하고 텍스트 검수로 폴백할 가능성이 큽니다.

수정 방향: 시각 검수용 엔드포인트와 모델 설정을 별도로 분리하거나, Ollama 네이티브 API에 맞는 시각 검수 호출을 구현해야 합니다. 구현 전까지는 시각 검수 옵션을 비활성 기능으로 명확히 표시하는 편이 낫습니다.

### P2: "확인 후 진행" UI가 실제로는 우회됨

파일: `assets/main.js`

`askSpecConfirmation()`이 즉시 `Promise.resolve(spec)`를 반환합니다. 그래서 명세 편집 카드와 취소 버튼은 코드에 남아 있지만 실제로는 표시되지 않습니다. 화면에는 여전히 "확인 후 진행"이라는 옵션이 있어 사용자 기대와 실제 동작이 어긋납니다.

수정 방향: 명세 확인 카드 흐름을 복구하거나, 현재처럼 자동 승인할 계획이라면 모드 이름과 죽은 UI 코드를 정리하는 것이 좋습니다.

### P3: 로컬 사용자 파일 ignore 누락

파일: `.gitignore`

`users.json`은 무시되지만 `user.json`은 무시되지 않아 로컬 사용자 설정이나 민감 정보가 실수로 커밋될 수 있었습니다.

수정 방향: `user.json`을 `.gitignore`에 추가합니다.

## 추가 메모

- `start_server.bat`는 Cloudflare tunnel `test-tunnel`을 직접 실행합니다. 기본 실행 스크립트로 커밋되면 다른 환경에서 실패하거나 로컬 서버가 외부에 노출될 수 있습니다.
- `server.py`에서는 `GZipMiddleware`가 제거되었지만, 주변 주석은 아직 gzip 동작을 설명하고 있습니다.
- `git diff --check`에서는 공백 오류는 없었고, 일부 파일의 LF에서 CRLF로 바뀔 수 있다는 경고만 있었습니다.

## 확인한 내용

- `assets/main.js`
- `assets/preview_core.js`
- `pipelines/dual_model_pipeline.py`
- `routes/chat.py`
- `server.py`
- `README.md`
- `.gitignore`

확인 명령:

```powershell
git status --short
git diff --check
```
