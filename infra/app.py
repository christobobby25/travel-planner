"""CDK app for the public chat API: API Gateway -> Lambda -> AgentCore Runtime, DynamoDB state.

Separate from agentcore/cdk, which the AgentCore CLI generates and manages.

  cd infra
  npx aws-cdk@2 diff
  npx aws-cdk@2 deploy

The runtime ARN is read from agentcore/.cli/deployed-state.json (written by `agentcore deploy`),
or pass -c runtime_arn=arn:aws:bedrock-agentcore:...
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_apigateway as apigw,
    aws_cloudwatch as cw,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
)
from constructs import Construct

INFRA = Path(__file__).resolve().parent
ROOT = INFRA.parent
BUILD = INFRA / ".build"


def runtime_arn_from_state() -> str | None:
    state = ROOT / "agentcore" / ".cli" / "deployed-state.json"
    if not state.exists():
        return None
    runtimes = json.loads(state.read_text())["targets"]["default"]["resources"]["runtimes"]
    return runtimes["travelplanner"]["runtimeArn"]


def bundle_worker() -> str:
    """Copy the worker code plus a current boto3. The Lambda runtime's built-in boto3 may predate
    the bedrock-agentcore client, so ship our own (pure Python, no Docker needed)."""
    out = BUILD / "worker"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(out), "boto3>=1.43"],
        check=True,
    )
    shutil.copy(INFRA / "lambda" / "worker" / "handler.py", out / "handler.py")
    return str(out)


class TravelApiStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, runtime_arn: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # Demo data: tables are deleted with the stack. Items expire via TTL.
        conversations = dynamodb.Table(
            self, "Conversations",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="turn", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        jobs = dynamodb.Table(
            self, "Jobs",
            partition_key=dynamodb.Attribute(name="job_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        worker = lambda_.Function(
            self, "Worker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(bundle_worker()),
            timeout=cdk.Duration.minutes(5),
            memory_size=256,
            retry_attempts=0,  # a retry would re-run the agent and save the turn twice
            environment={
                "JOBS_TABLE": jobs.table_name,
                "CONVERSATIONS_TABLE": conversations.table_name,
                "AGENTCORE_RUNTIME_ARN": runtime_arn,
            },
            log_group=logs.LogGroup(self, "WorkerLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=cdk.RemovalPolicy.DESTROY),
        )
        jobs.grant_read_write_data(worker)
        conversations.grant_read_write_data(worker)
        worker.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[runtime_arn, f"{runtime_arn}/*"],
        ))

        api_fn = lambda_.Function(
            self, "Api",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(INFRA / "lambda" / "api")),
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
            environment={"JOBS_TABLE": jobs.table_name, "WORKER_FUNCTION": worker.function_name},
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.TWO_WEEKS,
                                    removal_policy=cdk.RemovalPolicy.DESTROY),
        )
        jobs.grant_read_write_data(api_fn)
        worker.grant_invoke(api_fn)

        api = apigw.RestApi(
            self, "TravelApi",
            rest_api_name="travel-planner-api",
            description="Travel planner chat API (async: POST /chat, then poll GET /chat/{job_id})",
            deploy_options=apigw.StageOptions(stage_name="prod"),
            cloud_watch_role=False,  # don't change the account-wide API Gateway logging role
        )
        integration = apigw.LambdaIntegration(api_fn)
        chat = api.root.add_resource("chat")
        chat.add_method("POST", integration, api_key_required=True)
        chat.add_resource("{job_id}").add_method("GET", integration, api_key_required=True)

        # Every request needs the API key, and the usage plan caps traffic so a leaked key
        # can't run up Bedrock costs. Polling counts toward the quota (~20 GETs per answer).
        key = api.add_api_key("DemoKey", api_key_name="travel-planner-demo")
        plan = api.add_usage_plan(
            "DemoPlan",
            name="travel-planner-demo",
            throttle=apigw.ThrottleSettings(rate_limit=5, burst_limit=10),
            quota=apigw.QuotaSettings(limit=2000, period=apigw.Period.DAY),
        )
        plan.add_api_stage(stage=api.deployment_stage)
        plan.add_api_key(key)

        self.add_dashboard(api, api_fn, worker, runtime_arn)

        cdk.CfnOutput(self, "ApiUrl", value=api.url)
        cdk.CfnOutput(self, "ApiKeyId", value=key.key_id,
                      description="aws apigateway get-api-key --include-value --api-key <id>")
        cdk.CfnOutput(self, "ConversationsTable", value=conversations.table_name)
        cdk.CfnOutput(self, "JobsTable", value=jobs.table_name)

    def add_dashboard(self, api, api_fn, worker, runtime_arn: str) -> None:
        """One page for the demo: traffic, latency, errors, tokens and which specialists run."""
        five_min = cdk.Duration.minutes(5)
        runtime_name = runtime_arn.split("/")[-1].rsplit("-", 1)[0] + "::DEFAULT"
        runtime_dims = {"Resource": runtime_arn, "Operation": "InvokeAgentRuntime", "Name": runtime_name}
        model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

        def runtime(metric, stat="Sum", label=None):
            return cw.Metric(namespace="AWS/Bedrock-AgentCore", metric_name=metric,
                             dimensions_map=runtime_dims, statistic=stat, period=five_min,
                             label=label or metric)

        def bedrock(metric, stat="Sum"):
            return cw.Metric(namespace="AWS/Bedrock", metric_name=metric,
                             dimensions_map={"ModelId": model_id}, statistic=stat, period=five_min)

        def custom(metric, **dims):
            return cw.Metric(namespace="TravelPlanner", metric_name=metric, dimensions_map=dims,
                             statistic="Sum", period=five_min, label=next(iter(dims.values()), metric))

        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="TravelPlanner",
                                 default_interval=cdk.Duration.hours(3))
        dashboard.add_widgets(cw.TextWidget(width=24, height=2, markdown=(
            "## Travel Planner\nAPI Gateway → api Lambda → worker Lambda → AgentCore Runtime "
            "(supervisor → flight / hotel / weather specialists over MCP) → Bedrock Claude Haiku 4.5")))
        dashboard.add_widgets(
            cw.GraphWidget(title="API requests and errors", width=8, left=[
                api.metric_count(period=five_min, label="Requests"),
                api.metric_client_error(period=five_min, label="4xx"),
                api.metric_server_error(period=five_min, label="5xx")]),
            cw.GraphWidget(title="Jobs (completed chat answers)", width=8, left=[
                custom("JobsSucceeded"), custom("JobsFailed")]),
            cw.GraphWidget(title="Specialist calls", width=8, stacked=True, left=[
                custom("SpecialistCalls", Specialist=name)
                for name in ("flight_specialist", "hotel_specialist", "weather_specialist")]),
        )
        dashboard.add_widgets(
            cw.GraphWidget(title="Agent latency (AgentCore, ms)", width=8, left=[
                runtime("Latency", "p50", "p50"), runtime("Latency", "p90", "p90"),
                runtime("Latency", "Maximum", "max")]),
            cw.GraphWidget(title="Worker Lambda duration (end-to-end, ms)", width=8, left=[
                worker.metric_duration(statistic="p50", period=five_min, label="p50"),
                worker.metric_duration(statistic="p90", period=five_min, label="p90")]),
            cw.GraphWidget(title="Errors", width=8, left=[
                runtime("SystemErrors", label="AgentCore system"),
                runtime("UserErrors", label="AgentCore user"),
                worker.metric_errors(period=five_min, label="worker Lambda"),
                api_fn.metric_errors(period=five_min, label="api Lambda")]),
        )
        dashboard.add_widgets(
            cw.GraphWidget(title="Bedrock tokens", width=12, left=[
                bedrock("InputTokenCount"), bedrock("OutputTokenCount")]),
            cw.GraphWidget(title="Bedrock model calls and latency", width=12,
                           left=[bedrock("Invocations")],
                           right=[bedrock("InvocationLatency", "p90")]),
        )


app = cdk.App()
runtime_arn = app.node.try_get_context("runtime_arn") or runtime_arn_from_state()
if not runtime_arn:
    raise SystemExit("No AgentCore runtime ARN: run `agentcore deploy` first or pass -c runtime_arn=...")
TravelApiStack(
    app, "TravelPlannerApi",
    runtime_arn=runtime_arn,
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1"),
    tags={"project": "travel-planner"},
)
app.synth()
