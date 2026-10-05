"""Load API keys from AWS Secrets Manager into env vars (no-op locally if TRAVEL_SECRET_ID unset)."""
import json
import os

import boto3


def load_secrets_into_env() -> None:
    secret_id = os.environ.get("TRAVEL_SECRET_ID")
    if not secret_id:
        return
    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    values = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    for key, value in values.items():
        os.environ.setdefault(key, value)
