# LiteLLM Cursor SDK 프록시 구성 가이드

Cursor 구독 모델을 **OpenAI-compatible API**로 노출하는 LiteLLM 프록시 설정 방법입니다.
RisuAI, Open WebUI, LangChain 등 OpenAI 형식을 지원하는 클라이언트에서 Cursor 모델을 사용할 수 있습니다.

## 개요

```
[클라이언트] ── OpenAI API ──► [LiteLLM Proxy :4001]
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
            cursor/* 모델                    vertex_ai/* 모델
         (Cursor SDK + Bridge)              (Google Vertex AI)
```

| 구성 요소 | 역할 |
|---|---|
| `.cursor/config.yaml` | 프록시 모델 목록 및 라우팅 설정 |
| `.cursor/cursor_llm.py` | LiteLLM CustomLLM provider (Cursor SDK 래퍼) |
| `.cursor/cursor_env.py` | `cursor.local.env` 환경 변수 로더 |
| `.cursor/cursor.local.env` | API 키 및 로컬 설정 (Git 제외) |
| `.cursor/requirements.txt` | Cursor SDK 의존성 |

## 사전 요구 사항

- Python 3.10+ 및 [uv](https://github.com/astral-sh/uv)
- LiteLLM 저장소 클론 및 `uv sync` 완료
- [Cursor](https://cursor.com) 설치 및 유료 구독 (Pro 이상 권장)
- Cursor **API Key** ([Dashboard → Integrations](https://cursor.com/dashboard?tab=integrations))

## 빠른 시작

### 1. Cursor SDK 설치

```powershell
cd G:\dev\litellm
uv pip install -r .cursor\requirements.txt
```

### 2. 환경 변수 설정

`.cursor/cursor.local.env.example`을 복사합니다.

```powershell
copy .cursor\cursor.local.env.example .cursor\cursor.local.env
```

`.cursor/cursor.local.env`를 열고 API 키를 입력합니다.

```env
CURSOR_API_KEY=crsr_xxxxxxxxxxxxxxxx
COORD_WAKE_MODEL=composer-2.5
```

> **주의:** `cursor.local.env`는 Git에 커밋되지 않습니다. API 키를 절대 공유하지 마세요.

### 3. 프록시 서버 실행

Windows에서는 UTF-8 인코딩을 설정한 뒤 실행합니다.

```powershell
$env:PYTHONIOENCODING='utf-8'
uv run litellm --config G:\dev\litellm\.cursor\config.yaml --port 4001 --host 127.0.0.1
```

서버가 시작되면 `http://127.0.0.1:4001`에서 OpenAI-compatible API를 제공합니다.

### 4. 동작 확인

```powershell
# 등록된 모델 목록
curl http://127.0.0.1:4001/v1/models

# 간단한 채팅 요청
curl http://127.0.0.1:4001/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"안녕"}]}'
```

## API 형식

LiteLLM 프록시는 **OpenAI Chat Completions API**와 호환됩니다.

| 항목 | 값 |
|---|---|
| Base URL | `http://127.0.0.1:4001` |
| Chat Completions | `POST /v1/chat/completions` |
| Models | `GET /v1/models` |
| API Key | 비워두거나 임의 값 (`master_key` 미설정 시) |
| 스트리밍 | **OFF 권장** (Cursor provider는 비스트리밍) |

요청 예시:

```json
{
  "model": "claude-sonnet-4-5",
  "messages": [
    {"role": "user", "content": "Hello"}
  ]
}
```

응답은 OpenAI 표준 형식(`choices[].message.content`)입니다.

## 등록된 모델

### Cursor SDK 모델 (`cursor/*`)

Cursor 로컬 브리지에서 사용 가능한 모델 ID입니다.
`config.yaml`의 `model_name`과 `cursor/<id>`가 1:1로 매핑됩니다.

| model_name | 설명 |
|---|---|
| `composer-2.5`, `composer-2` | Cursor Composer |
| `claude-sonnet-4-5`, `claude-sonnet-4-6`, `claude-sonnet-5`, `claude-sonnet-4` | Claude Sonnet 계열 |
| `claude-opus-4-8` ~ `claude-opus-4-5` | Claude Opus 계열 |
| `claude-haiku-4-5`, `claude-fable-5` | Claude Haiku / Fable |
| `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.2`, `gpt-5.1`, `gpt-5-mini` 등 | GPT 계열 |
| `gemini-3.1-pro`, `gemini-3-flash`, `gemini-3.5-flash`, `gemini-2.5-flash` | Gemini 계열 |
| `grok-4.3`, `grok-build-0.1` | Grok |
| `kimi-k2.7-code`, `kimi-k2.5`, `glm-5.2` | 기타 |

별칭:

| model_name | 실제 모델 |
|---|---|
| `helena` | `composer-2.5` |

전체 목록은 `GET /v1/models`로 확인하세요.

### Vertex AI 모델 (`vertex_ai/*`)

| model_name | 실제 백엔드 |
|---|---|
| `claude-3-5-sonnet-20241022` | `vertex_ai/gemini-3.1-pro-preview` |

> Vertex 모델은 Google Cloud 프로젝트 인증(`vertex_project`, `vertex_location`)이 필요합니다.
> `config.yaml`의 프로젝트 ID를 본인 환경에 맞게 수정하세요.

## 모델 추가 방법

`.cursor/config.yaml`에 항목을 추가합니다. **`model_name`과 `cursor/<id>`의 ID는 Cursor 브리지에서 허용하는 이름과 정확히 일치**해야 합니다.

```yaml
  - model_name: claude-sonnet-4-5
    litellm_params:
      model: cursor/claude-sonnet-4-5
      api_key: os.environ/CURSOR_API_KEY
      drop_params: true
```

### 사용 가능한 모델 ID 확인

Cursor REST API(`/v0/models`)와 로컬 SDK 브리지의 모델 목록은 **다를 수 있습니다**.
반드시 SDK 브리지 기준 목록을 사용하세요.

에러 메시지에 `Available models: ...` 목록이 포함되면, 그 ID를 그대로 `cursor/<id>`에 사용합니다.

예시 (SDK 브리지 기준):

```
composer-2.5, claude-sonnet-4-5, claude-sonnet-4-6, claude-opus-4-8, gpt-5.5, ...
```

> **잘못된 예:** `claude-4.5-sonnet-thinking`, `claude-opus-4-8-thinking-high`
> (REST API 전용 이름 — SDK 브리지에서 거부됨)

설정 변경 후 프록시를 재시작합니다.

```powershell
# 4001 포트 프로세스 종료 후 재실행
$p = Get-NetTCPConnection -LocalPort 4001 -State Listen -ErrorAction SilentlyContinue |
     Select-Object -First 1 -ExpandProperty OwningProcess
if ($p) { Stop-Process -Id $p -Force }

$env:PYTHONIOENCODING='utf-8'
uv run litellm --config G:\dev\litellm\.cursor\config.yaml --port 4001 --host 127.0.0.1
```

## 환경 변수 참조

`.cursor/cursor.local.env`에서 설정할 수 있는 변수입니다.

| 변수 | 필수 | 설명 |
|---|---|---|
| `CURSOR_API_KEY` | ✅ | Cursor API Key (`crsr_...`) |
| `COORD_WAKE_MODEL` | | 기본 모델 (미지정 시 `composer-2.5`) |
| `CURSOR_CWD` | | Cursor SDK 에이전트 작업 디렉터리 |
| `CURSOR_SDK_BRIDGE_URL` | | 실행 중인 Cursor 브리지 URL (Windows 권장) |
| `CURSOR_SDK_BRIDGE_TOKEN` | | 브리지 인증 토큰 |

### API Key vs Bridge Token

| 종류 | 용도 |
|---|---|
| **CURSOR_API_KEY** | Cursor 클라우드 API 인증. 모델 호출에 필수 |
| **CURSOR_SDK_BRIDGE_TOKEN** | 로컬 Cursor IDE 브리지 연결용. IDE가 이미 실행 중일 때 사용 |

대부분의 경우 `CURSOR_API_KEY`만 설정하면 됩니다.

## Windows 특이 사항

Windows에서 Cursor SDK가 브리지를 자동 실행할 때 `WinError 10038`이 발생할 수 있습니다.
`.cursor/cursor_llm.py`에 Windows 전용 브리지 런처가 포함되어 있어 이 문제를 우회합니다.

그래도 실패하면 Cursor IDE를 실행한 뒤 브리지에 직접 연결하세요.

```env
CURSOR_SDK_BRIDGE_URL=http://127.0.0.1:PORT
CURSOR_SDK_BRIDGE_TOKEN=your-bridge-token
```

## RisuAI 연동

RisuAI **Custom API** 설정:

| 항목 | 값 |
|---|---|
| API URL | `http://127.0.0.1:4001/v1/chat/completions` |
| Format | OpenAI Compatible |
| Model | `claude-sonnet-4-5`, `composer-2.5`, `helena` 등 |
| API Key | 비워두기 |
| Streaming | OFF |

**토크나이저 설정:**

| 모델 계열 | 토크나이저 |
|---|---|
| Claude / Composer (`claude-sonnet-*`, `composer-*`, `helena`) | **Claude** |
| Gemini (`gemini-*`, Vertex alias) | **Gemma** |
| GPT (`gpt-*`) | **Tiktoken (OpenAI)** |

> Custom API 설정 화면에는 `Unknown` 옵션이 없습니다. 위 표를 참고하세요.

## 트러블슈팅

### `Cannot use this model: ...`

모델 ID가 SDK 브리지 목록과 일치하지 않습니다.
에러의 `Available models:` 목록에서 정확한 ID를 확인하고 `config.yaml`을 수정하세요.

### `Cursor API key not found`

`.cursor/cursor.local.env`에 `CURSOR_API_KEY`가 설정되어 있는지 확인하세요.

### `Cursor SDK local bridge failed to start`

1. Cursor IDE가 설치되어 있는지 확인
2. `CURSOR_SDK_BRIDGE_URL` / `CURSOR_SDK_BRIDGE_TOKEN`으로 기존 브리지에 연결
3. Linux/macOS에서는 자동 브리지가 더 안정적으로 동작

### `reasoning_effort` / `think` 파라미터 오류

일부 클라이언트가 Cursor provider가 지원하지 않는 파라미터를 전송합니다.
`config.yaml`의 `drop_params: true`로 대부분 무시되지만, 클라이언트에서 해당 옵션을 끄는 것이 좋습니다.

### 프록시 배너 깨짐 (Windows)

```powershell
$env:PYTHONIOENCODING='utf-8'
```

## 파일 구조

```
README.ko.md              ← 이 문서 (저장소 루트)
.cursor/
├── config.yaml           ← LiteLLM 프록시 설정
├── cursor_llm.py         ← Cursor CustomLLM provider
├── cursor_env.py         ← 환경 변수 로더
├── cursor.local.env      ← API 키 (Git 제외, 로컬 전용)
├── cursor.local.env.example
└── requirements.txt      ← cursor-sdk 의존성
```

## 아키텍처

1. 클라이언트가 `POST /v1/chat/completions`로 OpenAI 형식 요청 전송
2. LiteLLM이 `model_list`에서 `model_name` → `cursor/<id>` 매핑
3. `cursor_llm.py`가 메시지를 프롬프트 텍스트로 변환
4. Cursor SDK `Agent.prompt()`가 로컬 브리지를 통해 Cursor 에이전트 실행
5. 결과 텍스트를 OpenAI `ModelResponse` 형식으로 반환

## 보안

- `cursor.local.env`는 `.gitignore`에 포함되어 Git에 올라가지 않습니다
- API Key를 `config.yaml`에 직접 작성하지 마세요 (`os.environ/CURSOR_API_KEY` 사용)
- 프록시를 외부에 노출할 경우 `master_key` 설정을 권장합니다

## 참고

- [LiteLLM Proxy 문서](https://docs.litellm.ai/docs/proxy/quick_start)
- [Cursor SDK](https://pypi.org/project/cursor-sdk/)
- [Cursor API Keys](https://cursor.com/dashboard?tab=integrations)
