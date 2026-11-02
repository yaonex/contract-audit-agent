"""Per-category vulnerability Discoverer."""
from audit_agent.agents.base import Agent, AgentClient
from audit_agent.agents.grounding import ground_findings
from audit_agent.agents.prompts import load_prompt
from audit_agent.context.source import SourceContext
from audit_agent.observability.tracer import Tracer
from audit_agent.schemas.finding import DiscovererOutput, Finding
from audit_agent.schemas.report import DiscoveryInfo
from audit_agent.tools.registry import ToolRegistry


def build_user_message(context: SourceContext) -> str:
    """Construct user message text from SourceContext."""
    parts: list[str] = [f"File: {context.file}"]
    if context.truncated and context.omitted_ranges:
        ranges_str = ", ".join(
            f"{start}-{end}" if start != end else f"{start}"
            for start, end in context.omitted_ranges
        )
        parts.append(f"Lines {ranges_str} were omitted and are not shown.")
    if context.imports:
        parts.append(
            f"Imported files are not included in the context: {', '.join(context.imports)}"
        )
    parts.append(f"```solidity\n{context.text}\n```")
    return "\n\n".join(parts)


class Discoverer:
    """Discovers vulnerabilities of a specific category using an LLM."""

    def __init__(
        self,
        category: str,
        prompt_version: str = "reentrancy.v1",
        *,
        client: AgentClient,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        max_tokens_cap: int = 16384,
        tracer: Tracer | None = None,
        timeout_s: float = 300.0,
        agent: Agent | None = None,
    ) -> None:
        self.category = category
        self.prompt_version = prompt_version
        self._system_prompt = load_prompt(prompt_version)
        self._agent = agent or Agent(
            client=client,
            registry=ToolRegistry([]),
            name=f"discoverer_{category}",
            temperature=temperature,
            max_tokens=max_tokens,
            max_tokens_cap=max_tokens_cap,
            tracer=tracer,
            timeout_s=timeout_s,
        )

    def run(self, context: SourceContext) -> tuple[list[Finding], DiscoveryInfo]:
        """Run the discoverer on a SourceContext and ground the results."""
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": build_user_message(context)},
        ]
        result = self._agent.run(messages, schema=DiscovererOutput)
        output = result.value or DiscovererOutput(findings=[])
        return ground_findings(
            output,
            context,
            self.category,
            retries=result.format_retries,
            truncations=result.truncations,
            duration_s=result.elapsed_s,
        )

