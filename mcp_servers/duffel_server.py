"""Custom MCP server exposing the Duffel flight-search REST API as agent tools.

Run standalone for testing:  python mcp_servers/duffel_server.py
Inspect with:                npx @modelcontextprotocol/inspector python mcp_servers/duffel_server.py
"""
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

DUFFEL_BASE_URL = "https://api.duffel.com"
mcp = FastMCP("duffel-flights")


def _headers() -> dict:
    token = os.environ.get("DUFFEL_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("DUFFEL_ACCESS_TOKEN is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
    }


def _summarize_offer(offer: dict) -> dict:
    """Trim Duffel's large offer payload down to what an LLM actually needs."""
    slices = []
    for s in offer.get("slices", []):
        segments = [
            {
                "flight": f"{seg['marketing_carrier']['iata_code']}{seg['marketing_carrier_flight_number']}",
                "from": seg["origin"]["iata_code"],
                "to": seg["destination"]["iata_code"],
                "departs": seg["departing_at"],
                "arrives": seg["arriving_at"],
            }
            for seg in s.get("segments", [])
        ]
        slices.append({
            "origin": s["origin"]["iata_code"],
            "destination": s["destination"]["iata_code"],
            "duration": s.get("duration"),
            "stops": max(len(segments) - 1, 0),
            "segments": segments,
        })
    return {
        "offer_id": offer["id"],
        "airline": offer["owner"]["name"],
        "total_amount": offer["total_amount"],
        "currency": offer["total_currency"],
        "slices": slices,
    }


@mcp.tool()
async def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    cabin_class: str = "economy",
    max_connections: int = 1,
    max_results: int = 5,
) -> dict:
    """Search flight offers.

    Args:
        origin: Origin IATA airport or city code, e.g. "DFW".
        destination: Destination IATA code, e.g. "LHR".
        departure_date: Outbound date, YYYY-MM-DD.
        return_date: Optional return date, YYYY-MM-DD, for round trips.
        adults: Number of adult passengers.
        cabin_class: economy, premium_economy, business, or first.
        max_connections: 0 for nonstop only, 1 allows one stop, etc.
        max_results: How many cheapest offers to return.
    """
    slices = [{"origin": origin, "destination": destination, "departure_date": departure_date}]
    if return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date})

    body = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": cabin_class,
            "max_connections": max_connections,
        }
    }
    async with httpx.AsyncClient(timeout=40) as client:
        resp = await client.post(
            f"{DUFFEL_BASE_URL}/air/offer_requests",
            params={"return_offers": "true", "supplier_timeout": 20000},
            headers=_headers(),
            json=body,
        )
    if resp.status_code >= 400:
        # Return the error to the agent instead of crashing, so it can explain or retry.
        return {"error": resp.status_code, "details": resp.json().get("errors", resp.text)}

    offers = resp.json()["data"].get("offers", [])
    offers.sort(key=lambda o: float(o["total_amount"]))
    return {"count": len(offers), "offers": [_summarize_offer(o) for o in offers[:max_results]]}


@mcp.tool()
async def get_offer(offer_id: str) -> dict:
    """Get up-to-date details and price for one flight offer by its offer_id."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{DUFFEL_BASE_URL}/air/offers/{offer_id}", headers=_headers())
    if resp.status_code >= 400:
        return {"error": resp.status_code, "details": resp.text}
    return _summarize_offer(resp.json()["data"])


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
