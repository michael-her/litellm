# Antigravity CLI LiteLLM 프록시

Google Antigravity CLI `agy`에 로그인한 Windows 사용자의 모델을 OpenAI Chat Completions API로 제공합니다. 기본 주소는 `http://127.0.0.1:14606/v1`입니다. 기존 Vertex 프록시 `14604`와 다른 프록시는 변경하지 않습니다.

## 실행 및 클라이언트 전환

`.antigravity/antigravity.local.env.example`을 `antigravity.local.env`로 복사하고 `ANTIGRAVITY_PROXY_KEY`를 임의의 비밀 키로 설정합니다. 키 파일은 Git에서 제외됩니다. 현재 설치에는 로컬 키가 생성되어 있습니다.

```powershell
& .antigravity\install-startup.ps1
& .antigravity\start-server.ps1
```

Startup의 `litellm.exe antigravity.lnk`는 숨긴 Windows PowerShell로 실행합니다. 설치를 반복해도 바로가기는 하나입니다. 서버가 이미 실행 중이면 추가 서버를 만들지 않습니다. 다른 프로세스가 포트를 사용하면 PID를 포함한 오류로 종료합니다. `server.pid`에는 venv 실행기가 아닌 실제 포트 소유 프로세스의 PID가 기록됩니다.

RisuAI 등의 Base URL을 `http://127.0.0.1:14604/v1`에서 `http://127.0.0.1:14606/v1`로 바꾸고 API Key에는 `antigravity.local.env`의 `ANTIGRAVITY_PROXY_KEY` 값을 입력합니다. 기존 Vertex 키와는 별도입니다. 모델 이름은 `/v1/models`에서 선택합니다. 이 작업은 클라이언트 설정을 자동 변경하지 않습니다.

`ANTIGRAVITY_PROXY_PORT`로 기본 포트 `14606`을 바꿀 수 있습니다. 기본 동시 agy 프로세스 수는 2(`ANTIGRAVITY_MAX_CONCURRENCY`), 대기 제한은 30초(`ANTIGRAVITY_QUEUE_TIMEOUT_S`), 실행 제한은 600초(`ANTIGRAVITY_TIMEOUT_S`)입니다. 프록시 자체의 요청 제한은 660초입니다.

## 모델 검색과 Vertex fallback

서버 시작 시 `agy models`를 실행하여 `config.template.yaml`에서 `config.generated.yaml`을 만듭니다. Claude 및 GPT-OSS를 포함한 모든 계정 모델 ID를 그대로 노출합니다. 검색 실패 시 마지막 정상 설정을 유지하며, 정상 설정이 없으면 시작을 중단합니다. `models.generated.json`은 공개 이름별 경로를 기록합니다.

agy와 `.vertex/config.yaml` 양쪽에 같은 이름이 있으면 agy를 우선 호출하고 실패 시 `http://127.0.0.1:14604/v1`의 동일 이름으로 fallback합니다. Vertex 키는 실행 시 `.vertex/vertex.local.env`에서 읽고 생성 설정에는 환경 변수 참조만 저장합니다. 프록시의 agy 재시도는 0회입니다. Vertex 서버 내부의 기존 재시도 정책은 그대로 적용됩니다.

Vertex 이름 중 `-low/-medium/-high/-xhigh` suffix가 없고 `<이름>-medium`이 agy 목록에 있으면 해당 medium ID로 전달합니다. 이 규칙을 우선 적용하고, 그 외 이름은 agy의 정확한 ID를 사용하거나 Vertex로 전달합니다. 이 규칙은 시작 시 모델 목록에서 자동 계산하며 제품군을 바꾸지 않습니다. 현재 `gemini-4-hydrogen`, `gemini-4-neon`, `gemini-4-argon`, `gemini-3.8-flash`가 각각 같은 이름의 `-medium` ID를 사용합니다. 공개 이름은 suffix 없이 유지하고 실패 시 Vertex의 같은 공개 이름으로 fallback합니다. 예를 들어 `vertex-fallback/gemini-4-hydrogen`은 Vertex `gemini-4-hydrogen`을 호출합니다.

대응하는 agy medium ID가 없는 Carbon, Gemini 3.5, `vertex-default`는 Vertex 전용으로 유지합니다. effort suffix가 붙은 이름을 다른 effort로 재해석하지 않습니다. `models.generated.json`에는 `agy:gemini-4-hydrogen-medium-with-vertex-fallback`처럼 실제 대상과 fallback 유무를 표시합니다. 내부 fallback 배포 이름 `vertex-fallback/<공개 이름>`도 LiteLLM의 모델 목록에 표시됩니다. 일반 클라이언트는 원래 공개 이름을 사용하면 됩니다.

양쪽이 실패한 실제 HTTP 429 응답에서 agy의 원래 오류와 Vertex 오류가 함께 남는 것을 확인했습니다. LiteLLM router의 기존 오류 결합 동작을 사용합니다.

stream 요청에서도 첫 delta 이전 agy 실패는 Vertex stream으로 fallback하며 HTTP 200과 정상 SSE 데이터를 반환합니다. 양쪽 실패 시 HTTP 429 JSON 오류에 agy 원인을 남깁니다. LiteLLM은 stream의 첫 데이터를 확인한 후 HTTP 응답을 시작하므로 별도 사전 실행 wrapper는 필요하지 않습니다. 첫 token 이후 오류도 SSE error로 전달하고 연결을 종료합니다. 이 경우 LiteLLM 자체의 continuation fallback 시도가 발생할 수 있습니다.

## 요청 처리와 도구 제한

요청마다 새 agy 프로세스와 빈 작업 디렉터리를 만들고 종료 시 삭제합니다. `--conversation`이나 `--dangerously-skip-permissions`는 사용하지 않습니다. system/developer/history/user 메시지를 역할과 이름을 유지하는 JSON Lines transcript로 렌더링하여 stdin의 단일 NDJSON user 이벤트로 보냅니다. 긴 대화는 아래 입력 한도 정책을 적용합니다. 응답은 새 assistant 차례입니다.

`agent.md`를 `~/.gemini/config/agents/litellm-antigravity.md`에 멱등 설치하고 `--agent litellm-antigravity --disable-slash-commands`를 사용합니다. 에이전트는 `tools: []`, `mainAgent: true`, `subagent: false`입니다. CLI init의 tools 필드는 전역 registry를 나열하지만, 빈 도구 목록 에이전트로 도구 호출을 요구한 실제 probe는 `NO_TOOLS`를 반환했고 도구/권한 step은 없었습니다. 실행 중 도구/권한 이벤트, step 또는 denied action이 발견되면 요청을 중단합니다. 이는 도구 없는 CLI 에이전트 구성이며 별도 OS 보안 sandbox를 설치하지 않습니다.

동일한 `Reply with OK.` 프롬프트의 입력 토큰은 기본 에이전트 **9,047**, 빈 도구 에이전트 **2,186**이었습니다. 계정/CLI/프롬프트 변화에 따라 값은 바뀝니다. 진단 로그는 `logs/probe-default.jsonl`, `logs/probe-minimal.jsonl`, `logs/probe-tools.jsonl`에 있습니다.

문자열 또는 `type: text` 콘텐츠만 지원합니다. 이미지, 오디오, tool 메시지, tools/functions/function calling은 LiteLLM 400 오류입니다. 동일 이름의 Vertex fallback이 있으면 해당 요청은 Vertex에서 처리될 수 있습니다. sampling 등 별도 지원하지 않는 생성 매개변수는 무시합니다. non-stream 응답은 `result.response`, stream 응답은 `agent_response.text_delta`만 사용하며 thinking text는 포함하지 않습니다. 사용량은 `result.usage`에서 가져옵니다. agy `output_tokens`에 이미 포함된 thinking token은 중복 합산하지 않습니다.

오류는 quota/rate limit=RateLimitError, 인증=AuthenticationError, 없는 모델=NotFoundError, 시간 초과=Timeout, 비정상 종료/파싱 실패=APIConnectionError로 변환합니다. 프로세스 오류에는 stderr 끝부분을 포함합니다. 시간 초과와 요청 취소 시 Windows 프로세스 트리를 종료합니다. 프록시 PID, 임시 요청 디렉터리, 서버 로그는 모두 이 폴더에 둡니다. agy 자체의 로그인/대화 저장 정책은 CLI가 관리합니다.

요청 디렉터리 삭제는 Windows의 일시적인 파일 잠금에 대비하여 최대 4회, 총 0.33초 대기하며 재시도합니다. 계속 실패하면 경로와 원인을 서버 로그에 남기고 원래 요청 오류를 유지합니다.

## 긴 대화 입력 한도

agy 1.5.13 바이너리의 기본 `max_tokens_per_user_input_step`은 64,000입니다. 변환 과정의 truncation 예산은 토큰당 3바이트를 사용하여 사용자 입력 step당 192,000 UTF-8 바이트가 되며, CLI가 추가한 문맥/태그도 포함합니다. ASCII probe에서 131,072바이트의 시작/중간/끝 식별자는 모두 보였지만 262,144바이트에서는 끝 식별자가 사라졌습니다. 경계 probe에서는 191,900 위치 식별자가 보이고 192,100 위치 식별자는 보이지 않았습니다. 약 194KB의 한글 입력도 끝 식별자를 잃었으므로 문자 수 기준으로 처리하면 안전하지 않습니다.

`content`의 text part 배열은 허용되지만 CLI의 `PromptText`가 하나의 문자열로 합치므로 한도를 우회하지 못합니다. 64KiB part 네 개로 나눈 256KiB 입력에서도 끝 식별자가 사라졌습니다. 여러 user 이벤트를 한 번에 써도 합쳐지지 않고 이벤트마다 별도 모델 turn이 실행됩니다. 확인한 CLI help/session 설정에는 이 내부 한도를 올리는 지원 옵션이 없었습니다. 도구를 추가하는 우회는 사용하지 않습니다.

따라서 프록시는 UTF-8로 렌더링한 transcript를 **180,000바이트** 이내로 제한하여 CLI 변환 여유를 확보합니다. 한도 이내는 그대로 전달합니다. 초과하면 모든 system/developer 메시지, 처음 두 user/assistant 대화 메시지, 마지막 user 메시지와 그 뒤 assistant prefill을 원문 그대로 유지하고, 나머지 중 오래된 메시지부터 **메시지 전체 단위로** 생략합니다. 모델에는 `N earlier messages were omitted`라는 명시적인 system 역할 문맥 안내를 추가하고 서버 로그에는 생략 수와 전달 바이트 수를 기록합니다. 메시지 본문이나 UTF-8 문자를 중간에서 자르지 않습니다.

유지해야 하는 메시지 자체가 예산에 들어가지 않으면 원문을 agy에 보내지 않고 명확한 400 오류를 반환합니다. 특히 system/developer 지침이나 마지막 user 메시지가 큰 경우 조용히 자르지 않습니다. 이 정책은 긴 대화 전체를 전달하는 해결책이 아니라, 지원되는 도구 없는 우회가 없는 현재 CLI에서 최신 입력을 보호하는 마지막 수단입니다. CLI 업데이트로 한도/설정이 바뀌면 재검증해야 합니다.

진단은 `tests/probe_limits.py <로그이름> <string|string-ko|parts> <UTF-8 목표바이트>`로 재실행할 수 있습니다. 예를 들어 `python .antigravity/tests/probe_limits.py check-128 string 131072`는 저렴한 Flash Low로 식별자를 확인합니다. 입력, stdout, stderr 및 요청 디렉터리의 agy 로그는 `logs/limits/`에 보존됩니다. 큰 probe는 토큰을 사용하므로 필요한 크기만 실행합니다.

`python .antigravity/tests/verify_long_context.py`는 220KB 이상 한글 혼합 대화를 실행 중인 14606에 실제 curl로 한 번 전송하고, 시작/중간/끝 식별자와 명시적인 생략 메시지 수를 검증합니다. 원본 전체가 보존되었다는 검사는 아니며, 허용된 문맥 축약 정책 이후 필요한 메시지가 살아 있는지 확인합니다.

## 검증

```powershell
& .venv\Scripts\python.exe -m pytest .antigravity\tests\test_antigravity.py -q --basetemp=.antigravity\logs\pytest-temp
& .venv\Scripts\python.exe .antigravity\tests\verify_live.py
```

`verify_live.py`는 실제 `curl.exe`를 호출하고 비밀 키를 출력하지 않습니다. 전체 본문과 헤더는 `logs/`에 저장합니다. unit test의 fake agy는 transcript, stream, final response, usage, 오류 종류, 도구 차단, 프로세스 트리 종료, 동시성, 임시 폴더 정리, 모델 검색/fallback 설정 및 마지막 정상 설정 보존을 검증합니다.

격리 테스트에는 `ANTIGRAVITY_CLI`와 JSON 배열 `ANTIGRAVITY_CLI_ARGS`로 fake CLI를 지정할 수 있습니다. 정상 Startup 실행에는 두 변수를 설정하지 않습니다. 이 설정은 인증을 우회하지 않습니다.

`tests/verify_fallback.py`는 14606이 비어 있을 때만 실행하는 독립 통합 테스트입니다. 실행 전 Antigravity 서버만 중단하고 완료 후 `start-server.ps1`로 복구해야 합니다. 별도 HTTP upstream fixture로 non-stream 및 stream 성공 fallback(HTTP 200, `VERTEX_FIXTURE_OK`), 양쪽 실패 시 agy 오류 보존, 첫 token 이후 오류의 SSE 전달/연결 종료를 검증합니다. fixture curl 제한은 요청당 20초입니다. pytest는 같은 검사를 별도 임시 포트에서 실행합니다. 임시 fixture config는 종료 시 삭제하고 요청/응답 증거는 `logs/`에만 남깁니다. 실제 Vertex에 대한 Argon/Neon fallback도 확인했지만 기존 Vertex 할당량으로 둘 다 HTTP 429였으므로, 실제 Vertex의 성공 응답은 검증하지 못했습니다.
