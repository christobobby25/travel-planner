"""Streamlit chat UI for the travel planner.

Run from the project root:  streamlit run ui/app.py

Modes, picked from .env (first match wins):
  TRAVEL_API_URL + TRAVEL_API_KEY  the public API (API Gateway -> Lambda -> AgentCore, history in DynamoDB)
  AGENTCORE_RUNTIME_ARN            the AgentCore runtime directly (this app sends the history)
  neither                          local: the supervisor runs in this process
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

API_URL = (os.environ.get("TRAVEL_API_URL") or "").rstrip("/")
API_KEY = os.environ.get("TRAVEL_API_KEY")
RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN")
MODE = "api" if API_URL and API_KEY else "runtime" if RUNTIME_ARN else "local"
REGION = os.environ.get("AWS_REGION", "us-east-1")

EXAMPLES = [
    "Weekend in Austin from DFW, leaving this Friday, back Sunday. Flights and weather.",
    "Plan 4 nights in Chicago from DFW, leaving next Thursday, 1 adult, hotel under $250/night.",
    "What's the weather in Seattle this week?",
]
SPECIALIST_LABELS = {
    "flight_specialist": "✈️ Flights",
    "hotel_specialist": "🏨 Hotels",
    "weather_specialist": "🌤️ Weather",
}


@st.cache_resource
def mcp_sessions():
    """Open the Duffel and AccuWeather MCP sessions once per server process."""
    from supervisor import open_mcp_sessions

    return open_mcp_sessions()


def ask_local(prompt: str) -> tuple[str, list[str]]:
    from supervisor import build_supervisor

    mcp_sessions()
    if "supervisor" not in st.session_state:
        st.session_state.supervisor = build_supervisor()
    agent = st.session_state.supervisor
    seen = len(agent.messages)
    answer = str(agent(prompt))
    used = [
        c["toolUse"]["name"]
        for m in agent.messages[seen:]
        for c in m["content"]
        if "toolUse" in c
    ]
    return answer, used


def history() -> list[dict]:
    """Earlier turns in the runtime's payload format (the current prompt is already appended)."""
    return [{"role": m["role"], "text": m["content"]} for m in st.session_state.messages[:-1]]


def ask_api(prompt: str) -> tuple[str, list[str]]:
    import httpx

    headers = {"x-api-key": API_KEY}
    with httpx.Client(timeout=15) as client:
        resp = client.post(f"{API_URL}/chat", headers=headers,
                           json={"message": prompt, "session_id": st.session_state.session_id})
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(2)
            job = client.get(f"{API_URL}/chat/{job_id}", headers=headers)
            job.raise_for_status()
            job = job.json()
            if job["status"] == "done":
                return job["reply"], job.get("specialists", [])
            if job["status"] == "error":
                raise RuntimeError(job.get("error", "the agent failed"))
    raise TimeoutError("no answer after 5 minutes")


def ask_deployed(prompt: str) -> tuple[str, list[str]]:
    import boto3
    from botocore.config import Config

    # A full trip plan takes 15-40s; boto3's default 60s read timeout is too close.
    client = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(read_timeout=300))
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=st.session_state.session_id,
        payload=json.dumps({"prompt": prompt, "history": history()}).encode(),
    )
    body = json.loads(resp["response"].read())
    return body["result"], body.get("specialists", [])


def new_trip() -> None:
    st.session_state.messages = []
    st.session_state.session_id = str(uuid.uuid4())  # AgentCore needs >= 33 chars
    st.session_state.pop("supervisor", None)


st.set_page_config(page_title="Travel Planner", page_icon="🧳", layout="centered")
if "messages" not in st.session_state:
    new_trip()

with st.sidebar:
    st.header("🧳 Travel Planner")
    st.caption("A supervisor agent delegates to flight, hotel and weather specialists over MCP.")
    if MODE == "api":
        st.success("Mode: public API (API Gateway → Lambda → AgentCore)")
    elif MODE == "runtime":
        st.success("Mode: deployed (AgentCore Runtime)")
    else:
        st.info("Mode: local (supervisor runs in this process)")
    st.divider()
    st.subheader("Try an example")
    for example in EXAMPLES:
        if st.button(example, use_container_width=True):
            st.session_state.pending = example
    st.divider()
    st.button("🔄 New trip", on_click=new_trip, use_container_width=True)
    st.caption("Flights: Duffel (test mode) · Hotels: LiteAPI (sandbox) · Weather: AccuWeather")

st.title("Plan a trip")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("used"):
            st.caption("Specialists used: " + " · ".join(SPECIALIST_LABELS.get(u, u) for u in dict.fromkeys(msg["used"])))

prompt = st.chat_input("Where to? Include origin, dates and budget.") or st.session_state.pop("pending", None)
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Checking flights, hotels and weather… (usually 15-40s)"):
            try:
                answer, used = {"api": ask_api, "runtime": ask_deployed, "local": ask_local}[MODE](prompt)
            except Exception as e:
                answer, used = f"⚠️ Something went wrong: `{type(e).__name__}: {e}`", []
        st.markdown(answer)
        if used:
            st.caption("Specialists used: " + " · ".join(SPECIALIST_LABELS.get(u, u) for u in dict.fromkeys(used)))
    st.session_state.messages.append({"role": "assistant", "content": answer, "used": used})
