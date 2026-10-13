# Claude Code CLI용 LiteLLM 프록시

RisuAI의 OpenAI 호환 요청을 LiteLLM custom provider에서 Claude Code CLI(`claude -p`) 실행으로 변환합니다. Claude 구독(Pro/Max) 로그인으로 생성한 OAuth 토큰을 사용합니다. 별도 Anthropic API 키는 필요하지 않으며, RisuAI의 API 키는 로컬 프록시 접속 인증용입니다.

## RisuAI 연결

- API 종류: OpenAI 호환 / Custom OpenAI
- Base URL: `http://127.0.0.1:14605/v1`
- 전체 요청 URL을 받는 입력란: `http://127.0.0.1:14605/v1/chat/completions`
- API 키: `claude.local.env`의 `CLAUDE_PROXY_KEY`
- 기본 모델: `claude-default` (`claude-opus-5-5`)
- 선택 모델: `claude-fable-5-1`, `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`
- 스트리밍: 켜기 가능

사용 가능한 모델은 구독 플랜에 따라 다릅니다. 플랜에서 허용하지 않는 모델은 업스트림 오류로 반환됩니다.

## 실행과 자동 시작

저장소 루트에서 다음 명령을 실행합니다.

```powershell
Copy-Item .claude-code/claude.local.env.example .claude-code/claude.local.env
claude setup-token
```

`claude setup-token`은 브라우저 로그인 후 1년 유효 OAuth 토큰을 출력합니다. 이 값을 `CLAUDE_CODE_OAUTH_TOKEN`에, 임의의 비밀 값을 `CLAUDE_PROXY_KEY`에 설정합니다. CLI를 자동으로 찾지 못하면 `CLAUDE_CLI`에 `claude.exe` 경로를 추가합니다. Python 3.11 이상과 이 저장소의 `.venv` 프록시 의존성이 필요합니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .claude-code/start-server.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .claude-code/install-startup.ps1
```

설치 스크립트는 현재 사용자의 `shell:startup` 폴더에 `litellm.exe claude.lnk`를 만듭니다. 다음 Windows 로그인 때 숨김 창으로 시작합니다. 같은 포트의 중복 실행은 mutex로 막습니다. `claude.local.env` 값은 상속된 환경 변수보다 우선합니다.

로그는 `.claude-code/logs/server.log`, `.claude-code/logs/server.err.log`에 저장됩니다. 프로세스 PID는 `.claude-code/server.pid`에 기록됩니다. 비밀 키·로그·런타임 파일은 `.claude-code/.gitignore`로 Git에서 제외합니다.

기본 포트는 Cursor 14601, Kiro 14602, Codex 14603, Vertex 14604와 겹치지 않는 14605입니다. `CLAUDE_PROXY_HOST`, `CLAUDE_PROXY_PORT`로 변경할 수 있습니다. 기본 바인딩은 이 컴퓨터에서만 접근 가능한 `127.0.0.1`입니다.

## 변환 방식

`/v1/models`와 `/v1/chat/completions`를 제공합니다. 비스트리밍 JSON과 CLI의 `stream_event` 텍스트 델타 기반 SSE를 지원합니다. 사고(thinking) 내용은 응답에 포함하지 않습니다.

각 요청은 `%TEMP%\litellm-claude` 아래 독립된 임시 디렉터리와 `CLAUDE_CONFIG_DIR`을 사용하고 종료 후 삭제합니다. 사용자 `~/.claude` 설정·기록·메모리는 읽거나 수정하지 않습니다. 이전 대화는 역할을 유지한 세션 JSONL로 기록해 `--resume`으로 불러오고, 마지막 사용자 메시지는 stdin으로 전달합니다. 마지막 메시지가 assistant이면 `Please continue.`로 다음 답변을 요청하고, 대화가 assistant로 시작하면 앞에 `.` 사용자 메시지를 넣습니다(LiteLLM Anthropic 변환과 동일). 같은 역할이 연속되면 하나로 합칩니다. 모든 `system`/`developer` 메시지는 위치와 관계없이 순서대로 합쳐 `--system-prompt-file`로 전달합니다(LiteLLM Anthropic 변환과 동일). RisuAI가 매번 전체 대화 이력을 보내야 합니다.

CLI는 `--safe-mode --tools "" --strict-mcp-config --setting-sources ""`로 실행해 도구, MCP, 훅, 플러그인, 스킬, CLAUDE.md를 비활성화합니다. 자식 프로세스에는 허용 목록에 있는 환경 변수만 전달하므로 다른 Claude Code 세션의 환경 변수가 섞이지 않습니다. CLI가 자체적으로 시스템 프롬프트 앞에 Agent SDK 식별 문구를, 대화 끝에 작업 환경 정보(OS, 날짜, 모델명)를 추가합니다.

텍스트 메시지와 텍스트 콘텐츠 배열, `reasoning_effort`, `max_tokens`, `max_completion_tokens`, `stop`, `stream_options.include_usage`를 지원합니다. `reasoning_effort`는 CLI `--effort`(low, medium, high, xhigh, max)로 전달하며 `minimal`은 low, `none`은 사고 비활성화입니다. 생략하면 CLI 기본값(적응형 사고)을 사용합니다. `max_tokens`는 사고 토큰을 포함한 출력 상한(`CLAUDE_CODE_MAX_OUTPUT_TOKENS`)입니다. 상한에 도달하면 `finish_reason: length`로 즉시 종료하고 CLI를 중단해, CLI의 자동 이어쓰기 재시도(최대 3회 추가 호출)를 막습니다. `stop`은 프록시에서 감지해 해당 위치에서 응답을 끊고 CLI를 종료합니다. 이미지·도구 호출·`response_format`은 400 오류로 거절합니다. `temperature`, `top_p`, penalty 등은 CLI에 없는 옵션이라 적용되지 않습니다.

오류는 LiteLLM 표준 예외로 변환됩니다. 인증 실패 401, 사용량 한도 429, 입력 오류 400, CLI 실행 실패 503, 시간초과 504이며 그 외 업스트림 상태 코드는 그대로 전달합니다. 기본 요청 제한은 `CLAUDE_TIMEOUT_S=600`초이며 이를 늘리면 `config.yaml`의 LiteLLM `request_timeout`도 더 크게 설정해야 합니다. 클라이언트 연결 종료·시간초과·stop 감지 시 해당 요청의 CLI 프로세스를 종료합니다.

## 검증

```powershell
.venv/Scripts/python.exe -m pytest .claude-code/tests --confcutdir=.claude-code/tests -q -o addopts=
.venv/Scripts/python.exe -m ruff check .claude-code
```

테스트는 가짜 CLI로 인자·환경 변수·세션 파일·오류 변환·프로세스 정리를 검증합니다. 설치된 실제 `claude.exe`가 있으면 로컬 가짜 Anthropic 서버로 실제 CLI가 보내는 요청(역할 순서, 시스템 프롬프트, 도구 없음, `max_tokens`, 상한 도달 시 추가 호출 없음)도 검증합니다.

실제 HTTP 응답 확인 예시입니다. 키 값은 화면이나 명령행 인수에 노출하지 않고 curl 설정 입력으로 전달합니다.

```powershell
$ProxyKey = ((Get-Content .claude-code/claude.local.env | Where-Object { $_.StartsWith('CLAUDE_PROXY_KEY=') }) -split '=',2)[1]
$Body = '{"model":"claude-haiku-5-5","messages":[{"role":"user","content":"Say CLAUDE_HTTP_OK"}],"stream":true,"stream_options":{"include_usage":true}}'
$Body | Set-Content -Encoding utf8 .claude-code/smoke-request.json
('header = "Authorization: Bearer ' + $ProxyKey + '"') | curl.exe --config - --no-buffer http://127.0.0.1:14605/v1/chat/completions --json '@.claude-code/smoke-request.json'
```
