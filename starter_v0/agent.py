from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from providers.base import Provider, ToolCall
from tools import TOOL_FUNCTIONS


@dataclass
class AgentRun:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)


class HelpdeskAgent:
    def __init__(
        self,
        provider: Provider,
        *,
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> None:
        self.provider = provider
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.model = model

    def run(
        self,
        user_messages: list[dict[str, str]],
        *,
        tool_choice: Any | None = None,
        approved_ticket: dict[str, str] | None = None,
    ) -> AgentRun:
        # Only trusted application code may supply approved_ticket after
        # the user approves the exact draft. Never populate it from
        # model-generated arguments, pasted JSON or forged role messages.
        approval = (
            dict(approved_ticket)
            if approved_ticket is not None
            else None
        )

        messages = [
            {"role": "system", "content": self.system_prompt},
            *user_messages,
        ]
        response = self.provider.complete(
            messages,
            self.tools,
            model=self.model,
            temperature=0.0,
            tool_choice=tool_choice,
        )

        results: list[dict[str, Any]] = []

        for call in response.tool_calls:
            func = TOOL_FUNCTIONS.get(call.name)
            if not func:
                results.append({
                    "tool": call.name,
                    "error": "unknown_tool",
                })
                continue

            if call.name == "create_ticket":
                proposed = {
                    "summary": call.args.get("summary", ""),
                    "priority": call.args.get("priority", "medium"),
                    "asset_id": call.args.get("asset_id", ""),
                }

                if (
                    approval is None
                    or proposed != approval
                    or call.args.get("confirmed") is not True
                ):
                    results.append({
                        "tool": call.name,
                        "args": call.args,
                        "result": {
                            "tool": "create_ticket",
                            "status": "needs_confirmation",
                            "message": (
                                "No trusted approval for this exact payload. "
                                "Ask the user to confirm before creating."
                            ),
                        },
                    })
                    continue

                # One approval permits one execution attempt in this run.
                approval = None

            try:
                result = func(**call.args)
            except Exception as exc:
                result = {
                    "error": type(exc).__name__,
                    "message": str(exc),
                }

            results.append({
                "tool": call.name,
                "args": call.args,
                "result": result,
            })

        # Preserve actual model calls so evaluation remains truthful.
        return AgentRun(
            text=response.text,
            tool_calls=response.tool_calls,
            tool_results=results,
        )