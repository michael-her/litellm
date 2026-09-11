# Codex App Server용 LiteLLM 프록시

RisuAI의 OpenAI 호환 요청을 LiteLLM custom provider에서 Codex App Server의 JSON-RPC로 변환합니다. Codex CLI의 기존 ChatGPT 로그인을 사용합니다. 별도 OpenAI API 키는 필요하지 않으며, RisuAI의 API 키는 로컬 프록시 접속 인증용입니다.

## RisuAI 연결

- API 종류: OpenAI 호환 / Custom OpenAI
- Base URL: `http://127.0.0.1:14603/v1`
- 전체 요청 URL을 받는 입력란: `http://127.0.0.1:14603/v1/chat/completions`
- API 키: `codex.local.env`의 `CODEX_PROXY_KEY`
- 기본 모델: `codex-default` (`gpt-6-astra`)
- 선택 모델: `codex-gpt-6-astra`, `codex-gpt-5.6-sol`, `codex-gpt-5.6-terra`, `codex-gpt-5.6-luna`, `codex-gpt-5.5`, `codex-gpt-5.3-codex-spark`
- 스트리밍: 켜기 가능

모델 목록은 설치된 Codex CLI 0.153.4의 `model/list` 응답을 기준으로 구성했습니다. 로그인 계정이나 Codex 버전이 바뀌면 사용 가능한 모델도 달라질 수 있습니다.

## 실행과 자동 시작

저장소 루트에서 다음 명령을 실행합니다.

```powershell
Copy-Item .codex/codex.local.env.example .codex/codex.local.env
```

`CODEX_PROXY_KEY`를 설정합니다. CLI를 자동으로 찾지 못하면 `CODEX_CLI`에 네이티브 `codex.exe` 경로를 추가합니다. Python 3.11 이상과 이 저장소의 `.venv` 프록시 의존성이 필요합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .codex/start-server.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .codex/install-startup.ps1
```

설치 스크립트는 현재 사용자의 `shell:startup` 폴더에 `litellm.exe codex.lnk`를 만듭니다. 다음 Windows 로그인 때 숨김 창으로 시작합니다. 같은 포트의 중복 실행은 mutex로 막습니다. 부팅 직후 로그인 전 실행하는 Windows 서비스는 아닙니다.

로그는 `.codex/logs/server.log`, `.codex/logs/server.err.log`에 저장됩니다. 프로세스 PID는 `.codex/server.pid`에 기록됩니다. 비밀 키·로그·런타임 파일은 `.codex/.gitignore`로 Git에서 제외합니다.

기본 포트는 Cursor 14601, Kiro 14602와 겹치지 않는 14603입니다. `CODEX_PROXY_HOST`, `CODEX_PROXY_PORT`로 변경할 수 있습니다. 기본 바인딩은 이 컴퓨터에서만 접근 가능한 `127.0.0.1`입니다.

## 변환 범위

`/v1/models`와 `/v1/chat/completions`를 제공합니다. 비스트리밍 JSON 및 실제 `item/agentMessage/delta` 기반 SSE를 지원하고, 스트림 종료와 사용량은 LiteLLM이 OpenAI 형식으로 직렬화합니다. 모든 스트림 청크에 같은 응답 ID를 사용합니다.

각 요청은 독립된 app-server 프로세스와 ephemeral 대화를 사용합니다. 이전 메시지는 `thread/inject_items`에 역할을 유지해 전달하고, 마지막 사용자 메시지로 `turn/start`를 호출합니다. 마지막 메시지가 사용자 역할이 아니면 전체 이력을 주입한 뒤 다음 assistant 답변을 요청합니다. RisuAI가 매번 전체 대화 이력을 보내야 합니다.

프록시 요청에는 별도 빈 작업 디렉터리, 읽기 전용 sandbox, 도구 비활성화 및 승인 거절을 적용합니다. 사용자 Codex 설정 파일은 수정하지 않습니다. 프록시 자식 프로세스에만 개발 작업용 지침, MCP, 앱, 플러그인 및 훅 비활성화 설정을 전달합니다. 클라이언트 연결 종료·시간초과·stop 문자열 감지 시 해당 요청의 자식 프로세스를 정리합니다.

텍스트 메시지와 텍스트 콘텐츠 배열, `reasoning_effort`, `stop`, `stream_options.include_usage`를 지원합니다. 이미지·도구 호출·`response_format`은 400 오류로 거절합니다. `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, penalty 등 Codex turn API에 없는 생성 옵션은 적용되지 않습니다. 따라서 RisuAI의 최대 출력 토큰 설정은 길이 상한을 보장하지 않습니다. 기본 요청 제한은 `CODEX_TIMEOUT_S=600`초이며 이를 늘리면 `config.yaml`의 LiteLLM `request_timeout`도 더 크게 설정해야 합니다.

## 검증

```powershell
.venv/Scripts/python.exe -m pytest .codex/tests --confcutdir=.codex/tests -q -o addopts=
.venv/Scripts/python.exe -m ruff check .codex
```

실제 HTTP 응답 확인 예시입니다. 키 값은 화면이나 명령행 인수에 노출하지 않고 curl 설정 입력으로 전달합니다.

```powershell
$ProxyKey = ((Get-Content .codex/codex.local.env | Where-Object { $_.StartsWith('CODEX_PROXY_KEY=') }) -split '=',2)[1]
$Body = '{"model":"codex-gpt-5.6-luna","messages":[{"role":"user","content":"Say CODEX_HTTP_OK"}],"stream":true,"stream_options":{"include_usage":true}}'
$Body | Set-Content -Encoding utf8 .codex/smoke-request.json
('header = "Authorization: Bearer ' + $ProxyKey + '"') | curl.exe --config - --no-buffer http://127.0.0.1:14603/v1/chat/completions --json '@.codex/smoke-request.json'
```

실제 ChatGPT 로그인으로 JSON과 SSE에서 `CODEX_HTTP_OK` 수신, `finish_reason: stop`, 토큰 사용량, `[DONE]`, CORS preflight, 인증 거절, 연결 종료 후 자식 프로세스 정리를 확인했습니다.

프로토콜 참고: [공식 Codex App Server 문서](https://learn.chatgpt.com/docs/app-server). 설치 버전별 정확한 스키마는 `codex app-server generate-json-schema --out <directory>`로 확인할 수 있습니다.
