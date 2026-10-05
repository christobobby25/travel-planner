"""Worker Lambda: run one chat job against the AgentCore runtime.

Invoked asynchronously by the API Lambda with {job_id, session_id, message}. Loads the session's
recent turns from DynamoDB, sends them with the new message to the runtime, saves both turns,
and marks the job done (or error). Async retries are disabled, so a failure is recorded once.
"""
import json
import os
import time

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config

DYNAMO = boto3.resource("dynamodb")
JOBS = DYNAMO.Table(os.environ["JOBS_TABLE"])
CONVERSATIONS = DYNAMO.Table(os.environ["CONVERSATIONS_TABLE"])
AGENTCORE = boto3.client("bedrock-agentcore", config=Config(read_timeout=280, retries={"max_attempts": 1}))
RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]

HISTORY_TURNS = 20  # last 10 user/assistant exchanges
CONVERSATION_TTL_SECONDS = 7 * 24 * 3600


def handler(event, context):
    job_id, session_id, message = event["job_id"], event["session_id"], event["message"]
    try:
        history = load_history(session_id)
        reply, specialists = ask_agent(session_id, message, history)
        save_turns(session_id, message, reply)
        finish(job_id, status="done", reply=reply, specialists=specialists)
        publish_metrics(succeeded=True, specialists=specialists)
    except Exception as e:
        print(f"Job {job_id} failed: {type(e).__name__}: {e}")
        finish(job_id, status="error", error="The travel agent failed to answer. Please try again.")
        publish_metrics(succeeded=False, specialists=[])


def load_history(session_id):
    resp = CONVERSATIONS.query(
        KeyConditionExpression=Key("session_id").eq(session_id),
        ScanIndexForward=False,
        Limit=HISTORY_TURNS,
    )
    return [{"role": t["role"], "text": t["text"]} for t in reversed(resp["Items"])]


def ask_agent(session_id, message, history):
    resp = AGENTCORE.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        # AgentCore requires >= 33 characters; session_id is >= 10.
        runtimeSessionId=f"travel-planner-session-{session_id}",
        payload=json.dumps({"prompt": message, "history": history}).encode(),
    )
    body = json.loads(resp["response"].read())
    if "result" not in body:
        raise RuntimeError(f"runtime returned no result: {body.get('error', body)}")
    return body["result"], body.get("specialists", [])


def save_turns(session_id, message, reply):
    now_ms = int(time.time() * 1000)
    expires_at = now_ms // 1000 + CONVERSATION_TTL_SECONDS
    with CONVERSATIONS.batch_writer() as batch:
        batch.put_item(Item={"session_id": session_id, "turn": now_ms, "role": "user",
                             "text": message, "expires_at": expires_at})
        batch.put_item(Item={"session_id": session_id, "turn": now_ms + 1, "role": "assistant",
                             "text": reply, "expires_at": expires_at})


def finish(job_id, status, reply=None, error=None, specialists=None):
    names = {"#s": "status"}
    values = {":s": status}
    update = "SET #s = :s"
    if reply is not None:
        update += ", reply = :r"
        values[":r"] = reply
    if specialists is not None:
        update += ", specialists = :sp"
        values[":sp"] = specialists
    if error is not None:
        update += ", #e = :e"
        names["#e"] = "error"
        values[":e"] = error
    JOBS.update_item(Key={"job_id": job_id}, UpdateExpression=update,
                     ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def publish_metrics(succeeded, specialists):
    """CloudWatch embedded metric format: Lambda ships these log lines as metrics, no API calls."""
    timestamp = int(time.time() * 1000)

    def emf(dimensions, metrics, **fields):
        print(json.dumps({
            "_aws": {"Timestamp": timestamp, "CloudWatchMetrics": [{
                "Namespace": "TravelPlanner",
                "Dimensions": [dimensions],
                "Metrics": [{"Name": name, "Unit": "Count"} for name in metrics],
            }]},
            **fields,
        }))

    emf([], ["JobsSucceeded", "JobsFailed"],
        JobsSucceeded=int(succeeded), JobsFailed=int(not succeeded))
    for name in specialists:
        emf(["Specialist"], ["SpecialistCalls"], Specialist=name, SpecialistCalls=1)
