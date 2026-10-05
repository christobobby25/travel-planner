"""API Lambda behind API Gateway: queue chat jobs and report their status.

POST /chat             {"message": str, "session_id": str (optional)} -> 202 {job_id, session_id, status}
GET  /chat/{job_id}    -> {job_id, session_id, status: pending|done|error, reply?, specialists?, error?}

The agent takes 25-40s, longer than API Gateway's ~29s limit, so POST only records the job and
hands it to the worker Lambda asynchronously. Clients poll GET until status is done or error.
"""
import json
import os
import re
import time
import uuid

import boto3

JOBS = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
LAMBDA = boto3.client("lambda")
WORKER_FUNCTION = os.environ["WORKER_FUNCTION"]

MAX_MESSAGE_CHARS = 2000
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{10,64}$")
JOB_TTL_SECONDS = 24 * 3600


def handler(event, context):
    try:
        if event["httpMethod"] == "POST":
            return create_job(event)
        if event["httpMethod"] == "GET":
            return get_job(event["pathParameters"]["job_id"])
        return respond(405, {"error": "method not allowed"})
    except Exception:
        print(f"Unhandled error for {event.get('httpMethod')} {event.get('path')}")
        raise


def create_job(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "body must be JSON"})
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return respond(400, {"error": "message must be a non-empty string"})
    if len(message) > MAX_MESSAGE_CHARS:
        return respond(400, {"error": f"message is limited to {MAX_MESSAGE_CHARS} characters"})
    session_id = body.get("session_id") or str(uuid.uuid4())
    if not isinstance(session_id, str) or not SESSION_ID.match(session_id):
        return respond(400, {"error": "session_id must be 10-64 letters, digits or dashes"})

    job_id = str(uuid.uuid4())
    now = int(time.time())
    JOBS.put_item(Item={
        "job_id": job_id,
        "session_id": session_id,
        "status": "pending",
        "created_at": now,
        "expires_at": now + JOB_TTL_SECONDS,
    })
    LAMBDA.invoke(
        FunctionName=WORKER_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({"job_id": job_id, "session_id": session_id, "message": message}),
    )
    return respond(202, {"job_id": job_id, "session_id": session_id, "status": "pending"})


def get_job(job_id):
    item = JOBS.get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        return respond(404, {"error": "job not found"})
    fields = ("job_id", "session_id", "status", "reply", "error", "specialists")
    return respond(200, {k: item[k] for k in fields if k in item})


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
