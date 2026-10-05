"""Weather agent (Strands) using AccuWeather's hosted MCP server (streamable HTTP)."""
import os

from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from agents.dates import TodayInSystemPrompt, with_today

ACCUWEATHER_MCP_URL = "https://dataservice.accuweather.com/mcp"

WEATHER_PROMPT = """You are a weather specialist for travelers. Use the weather tools to get
current conditions and the forecast for the destination and travel dates. Mention anything
that affects packing or plans (rain, heat, storms, alerts). Keep it brief."""

# AccuWeather exposes 26 tools; the agent only needs location lookup, forecasts and alerts.
ACCUWEATHER_ALLOWED_TOOLS = [
    "search_locations",        # place name -> location key (needed by every other call)
    "get_current_conditions",
    "get_daily_forecast",
    "get_hourly_forecast",
    "get_weather_alerts",
]

accuweather_client = MCPClient(
    lambda: streamablehttp_client(
        ACCUWEATHER_MCP_URL,
        headers={"Authorization": f"Bearer {os.environ['ACCUWEATHER_API_KEY']}"},
    ),
    tool_filters={"allowed": ACCUWEATHER_ALLOWED_TOOLS},
)


def build_weather_agent(model: BedrockModel) -> Agent:
    return Agent(
        model=model,
        system_prompt=with_today(WEATHER_PROMPT),
        hooks=[TodayInSystemPrompt(WEATHER_PROMPT)],
        tools=accuweather_client.list_tools_sync(),
        callback_handler=None,  # answer goes back to the supervisor; don't also stream it
    )
