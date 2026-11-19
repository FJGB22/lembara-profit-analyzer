import json
import os

from .analysis import Report

DEFAULT_MODEL = "claude-haiku-5-5"

INSTRUCTIONS = """You are helping a small handmade shop owner understand their numbers.
Use only the figures in the data below. Do not invent numbers, benchmarks or causes you cannot see in the data.
Write in plain English for a non-technical seller, at most 200 words, in this order:
1. One sentence on overall profit and margin.
2. Products or orders that lose money or earn very little per unit, and which cost line causes it (fees, product cost or shipping).
3. Items that need restocking.
4. Up to three concrete actions the seller could consider, each tied to a number in the data.
If something cannot be concluded from the data, say so."""


def build_prompt(report: Report) -> str:
    data = {
        "summary": report.summary,
        "by_product": report.by_product[
            ["product", "units_sold", "sales", "net_profit", "profit_per_unit", "cost_used", "cost_source", "status"]
        ].to_dict(orient="records"),
        "by_channel": report.by_channel.to_dict(orient="records"),
        "losing_orders": report.losing_orders.drop(columns=["date"]).to_dict(orient="records"),
        "warnings": report.warnings,
    }
    return f"{INSTRUCTIONS}\n\nDATA (USD):\n{json.dumps(data, indent=2, default=str)}"


def write_ai_summary(report: Report, model: str | None = None) -> str:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Set the ANTHROPIC_API_KEY environment variable to use --ai")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        max_tokens=600,
        messages=[{"role": "user", "content": build_prompt(report)}],
    )
    return "".join(block.text for block in message.content if block.type == "text").strip()
