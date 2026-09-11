import json
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
Path("child.pid").write_text(str(os.getpid()))


def send(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def event(method: str, **params: object) -> None:
    send({"method": method, "params": {"threadId": "thread-test", "turnId": "turn-test", **params}})


for line in sys.stdin:
    request = json.loads(line)
    with Path("requests.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(request, ensure_ascii=False) + "\n")
    method = request.get("method")
    if "id" not in request:
        continue
    result = {}
    if method == "initialize":
        if mode == "malformed":
            sys.stdout.write("invalid-json\n")
            sys.stdout.flush()
            continue
        if mode == "hang-init":
            time.sleep(30)
    elif method == "account/read":
        result = {"account": None if mode == "no-auth" else {"type": "chatgpt"}}
    elif method == "thread/start":
        result = {"thread": {"id": "thread-test"}}
    elif method == "turn/start":
        if mode == "eof":
            sys.exit(0)
        event("item/agentMessage/delta", itemId="a", delta="안녕 ")
        result = {"turn": {"id": "turn-test", "status": "inProgress"}}
    send({"id": request["id"], "result": result})
    if method == "turn/start":
        if mode == "hang-turn":
            time.sleep(30)
        if mode == "approval":
            send({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})
            continue
        event("item/agentMessage/delta", itemId="a", delta="세계<ST")
        event("item/agentMessage/delta", itemId="a", delta="OP>hidden")
        event("item/completed", item={"id": "a", "type": "agentMessage", "text": "안녕 세계<STOP>hidden"})
        event(
            "thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": 21, "outputTokens": 8, "totalTokens": 29}}
        )
        event(
            "turn/completed",
            turn={
                "id": "turn-test",
                "status": "failed" if mode == "failed" else "completed",
                "error": {"message": "Rate limit reached", "codexErrorInfo": "usageLimitExceeded"}
                if mode == "failed"
                else None,
            },
        )
