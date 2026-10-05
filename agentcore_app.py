"""AgentCore Runtime entrypoint (Phase 4).

  agentcore configure --entrypoint agentcore_app.py
  agentcore launch            # older toolkit versions; newer ones use `agentcore deploy`
  agentcore invoke '{"prompt": "Weekend in Austin from DFW, Nov 7-9"}'

In AWS, read API keys from Secrets Manager instead of .env (see secrets_loader.py).
"""
from bedrock_agentcore import BedrockAgentCoreApp

from secrets_loader import load_secrets_into_env

load_secrets_into_env()

from supervisor import build_supervisor, open_mcp_sessions  # noqa: E402

app = BedrockAgentCoreApp()
_stack = open_mcp_sessions()  # keep MCP sessions open for the container's lifetime


def _history_to_messages(history: list) -> list:
    """[{"role": "user"|"assistant", "text": "..."}] -> Converse messages, starting with a user turn."""
    messages = [
        {"role": turn["role"], "content": [{"text": turn["text"]}]}
        for turn in history
        if turn.get("role") in ("user", "assistant") and turn.get("text")
    ]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


@app.entrypoint
def invoke(payload):
    """Payload: {"prompt": str, "history": [{"role", "text"}, ...] (optional)}.

    The caller (the API worker Lambda) owns conversation history in DynamoDB, so each request
    gets a fresh supervisor built from that history and nothing is shared between sessions.
    """
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "prompt must be a non-empty string"}
    supervisor = build_supervisor(_history_to_messages(payload.get("history") or []))
    seen = len(supervisor.messages)
    result = str(supervisor(prompt))
    # Which specialists ran, in call order: shown in the UI and published as a metric by the worker.
    specialists = [
        block["toolUse"]["name"]
        for message in supervisor.messages[seen:]
        for block in message["content"]
        if "toolUse" in block
    ]
    return {"result": result, "specialists": specialists}


if __name__ == "__main__":
    app.run()
