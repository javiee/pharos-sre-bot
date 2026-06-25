
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from pydantic import BaseModel, Field

from .config import settings
from .vectorstore import RetrievedChunk, VectorStore
from .tools.registry import SignalProvider

import logging
from .logging import TRACE  # registers the TRACE level + logger.trace()

logger = logging.getLogger(__name__)

class Diagnosis(BaseModel):
    """The structured output the agent returns. Used as the LLM output schema."""
    likely_cause: str = Field (description="The most likely cause of the problem")
    confidence: float = Field(description="Confidence 0.0-1.0 in the likely cause.")
    checks_performed: list[str] = Field(description="A list of checks the agent performed.")
    next_steps: list[str] = Field(description="Recommended remediation steps")
    sources: list[str] = Field(description="List of sources (runbook snippets) used to reach the diagnosis.")

class AgentState(TypedDict, total=False):
    """The shared state that flows through every node.

    `total=False` means not every key must be present at every step — nodes fill
    keys in as the run progresses.
    """
    alert: str                       # the incoming alert text (input)
    category: str                    # set by classify
    chunks: list[RetrievedChunk]     # set/extended by retrieve
    diagnosis_notes: str             # set by diagnose
    needs_more: bool                 # set by diagnose; drives the loop
    steps: int                       # loop counter, bounds the run
    result: Diagnosis                # set by summarize (final output)
    signal_request: "SignalRequest"  # what diagnose asked to fetch
    signals_text: str
    alert_time: str    
    namespace: str                   # ISO time the alert was received (set by the webhook)

class SignalRequest(BaseModel):
    """A request from the LLM to fetch one Grafana signal.

    The LLM emits this as JSON during diagnose. Python validates it and, only
    if `tool` is set, executes that MCP tool. Read-only by construction: the
    agent only ever asks to read, and the service account can only read.
    """

    tool: str | None = Field(
        default=None,
        description="Name of the Grafana MCP tool to call, or null if none needed.",
    )
    arguments: dict = Field(
        default_factory=dict,
        description="Arguments for the tool, matching its input schema.",
    )
    reason: str = Field(default="", description="Why this signal helps.")

def _parse_alert_time(value: str | None) -> datetime:
    """Parse the ISO alert-receive time, falling back to 'now' if absent/bad."""
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def build_graph(store: VectorStore, client: OpenAI, signals: SignalProvider | None = None):

    def classify(state: AgentState) -> AgentState:

        alert = state["alert"]
        logger.info("classify: category=%r", alert.splitlines()[0])
        return {"category": alert, "steps": 0}
    
    def retrieve(state: AgentState) -> AgentState:

        hits = store.search(state["category"], top_k=4)
        existing = state.get("chunks", [])
        logger.info("retrieve: %d chunks (had %d)", len(hits), len(existing))
        if logger.isEnabledFor(TRACE):
            for c in hits:
                logger.trace("retrieved chunk [%s]: %s", c.source, c.text)
        return {"chunks": existing + hits}
    
    def gather_signals(state: AgentState) -> AgentState:
        """Execute the ONE Grafana tool the LLM requested; store its output.

        Read-only: we only ever run the tool the model named, and the service
        account is scoped read-only, so even a wrong choice cannot mutate Grafana.
        """
        request: SignalRequest = state.get("signal_request", SignalRequest())
        assert signals is not None

        args = dict(request.arguments or {})
        if request.tool == "query_prometheus":
            # The tool wants the PromQL under `expr`; the model often emits `query`.
            if "expr" not in args and "query" in args:
                args["expr"] = args.pop("query")
            # 10-minute range ENDING when the alert was received. We express it
            # relative to now (e.g. "now-30s", "now-630s") because Grafana's time
            # parser accepts relative durations cleanly but rejects RFC3339 with
            # microseconds. Anchored to the alert time, not just "now".
            now = datetime.now(timezone.utc)
            secs_ago = max(0, int((now - _parse_alert_time(state.get("alert_time"))).total_seconds()))
            args["queryType"] = "range"
            args["endTime"] = "now" if secs_ago == 0 else f"now-{secs_ago}s"
            args["startTime"] = f"now-{secs_ago + 600}s"
            args.setdefault("stepSeconds", 30)
        
        logger.info(
            "gather_signals: calling %r (step %d of %d)",
            request.tool, state.get("steps", 0), settings.max_steps,
        )
        logger.debug("gather_signals: arguments=%s", args)
        try:
            output = signals.call_tool(request.tool, args)
            logger.debug("gather_signals: %r returned:\n%s", request.tool, output)
        except Exception as exc:  # a bad tool/args shouldn't kill the whole run
            logger.exception("gather_signals: tool %r failed", request.tool)
            output = f"(signal '{request.tool}' failed: {exc})"
        prior = state.get("signals_text", "")
        block = f"### {request.tool}({args})\n{output}"

        return {"signals_text": (prior + "\n\n" + block).strip()}
        
    
    def diagnose(state: AgentState) -> AgentState:

        # This is where the LLM is called to produce a diagnosis.
        # It should return a structured Diagnosis object.
        # For now, we just simulate it.
        if logger.isEnabledFor(TRACE):
            logger.trace("diagnose: entering with state=%s", state)
        context = "\n\n---\n\n".join(
            f"[{c.source}] {c.text}" for c in state.get("chunks", [])
        )
        gathered = state.get("signals_text", "")
        # print(f"Diagnosing with context:\n{context}")
        tool_menu = ""
        if signals is None:
            completion = client.chat.completions.create(
                model=settings.model,
                max_tokens=1024,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            notes = completion.choices[0].message.content or ""
            return {
                "diagnosis_notes": notes,
                "needs_more": False,
                "steps": state.get("steps", 0) + 1,
            }
        
        if signals is not None:
            
            tools = signals.list_tools()
            logger.debug(
                "diagnose: %d tools available: %s",
                len(tools), [t.name for t in tools],
            )
            def _fmt(t):
                props = (t.parameters or {}).get("properties", {})
                req   = set((t.parameters or {}).get("required", []))
                params = ", ".join(f"{n}{'*' if n in req else ''}" for n in props) or "none"
                return f"- {t.name}({params}): {t.description}"
            listed = "\n".join(_fmt(t) for t in tools)
            logger.trace("diagnose: tool menu:\n%s", listed)
            tool_menu = (
                "\n\nYou may request ONE live Grafana signal if the runbooks are "
                "insufficient. Available READ-ONLY tools:\n"
                f"{listed}\n\n"
                "Tool signatures show argument names as tool(arg*, arg2); * marks "
                "required args. Use those exact names in `arguments`.\n"
                "Return ONLY a JSON object with keys: tool (string or null), "
                "arguments (object), reason (string). Set tool to null if you have "
                "enough information to diagnose now."
            )
        ns = state.get("namespace")
        namespace_promt =  (
          f"\n\nThe affected namespace is '{ns}'. For Kubernetes tools,pass "
          f'"namespace": "{ns}" in arguments. For query_prometheus, include '
          f'`namespace="{ns}"` as a label in the PromQL expr.'
        )
        prompt = (
            "You are an on-call infrastructure assistant. Given an alert and "
            "runbook excerpts, reason about the likely cause.\n\n"
            f"ALERT:\n{state['alert']}\n\n"
            f"RUNBOOKS:\n{context or '(none retrieved)'}\n\n"
            f"GATHERED SIGNALS:\n{gathered or '(none yet)'}"
            f"{namespace_promt}\n\n"
            f"{tool_menu}"
        )
        logger.trace("diagnose: LLM prompt (%d chars):\n%s", len(prompt), prompt)
        completion = client.chat.completions.create(
            model=settings.model,
            max_tokens=2048,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object", "json_schema": SignalRequest.model_json_schema()},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = completion.choices[0].message.content or "{}"
        logger.trace("diagnose: raw LLM response: %s", raw)
        try:
            request = SignalRequest.model_validate_json(raw)
        except Exception as e:
            logger.exception("diagnose: failed to parse signal request JSON: %s", raw)
            request = SignalRequest()
        if request.tool:
            # INFO: the model asked for a live signal — a key story beat.
            logger.info("diagnose: LLM requested signal %r (step %d)", request.tool, state.get("steps", 0) + 1)
        else:
            logger.info("diagnose: no signal needed, ready to summarize (step %d)", state.get("steps", 0) + 1)
        # DEBUG: the fully parsed SignalRequest object (tool 
        logger.debug("diagnose: parsed request=%r", request)
        return {
            "diagnosis_notes": f"LLM requested signal: {request.tool} with args {request.arguments}. Reason: {request.reason}",
            "needs_more": request.tool is not None,
            "signal_request": request,
            "steps": state.get("steps", 0) + 1,
        }

    
    def summarize(state: AgentState) -> AgentState:
        """Produce the final structured Diagnosis.

        On this stack strict json_schema hangs, so we ask for a JSON OBJECT with
        thinking disabled, describe the fields in the prompt, and validate the
        returned text against the Diagnosis model ourselves.
        """
        sources = sorted({c.source for c in state.get("chunks", [])})
        context = "\n\n---\n\n".join(
          f"[{c.source}] {c.text}" for c in state.get("chunks", [])
        )
        signals = state.get("signals_text", "")
        prompt = (
          "You are an on-call infrastructure assistant. Write a clear,explanatory "
          "diagnosis of the alert below, grounded in the runbook excerpts and the "
          "live signals gathered. Return ONLY a JSON object with these keys:\n"
          "summary (string: 3-5 sentences explaining what is happening and why),\n"
          "likely_cause (string),\n"
          "reasoning (string: how the evidence leads to the cause; reference the\n"
          "specific observations and runbook guidance you used),\n"
          "confidence (number 0-1) and confidence_rationale (string: why that number),\n"
          "evidence (array of strings: concrete observations, each citing its source),\n"
          "checks_performed (array of strings),\n"
          "next_steps (array of strings: each says what to do AND what to look for),\n"
          "sources (array of strings).\n\n"
          f"ALERT:\n{state['alert']}\n\n"
          f"RUNBOOKS:\n{context or '(none retrieved)'}\n\n"
          f"GATHERED SIGNALS:\n{signals or '(none)'}\n\n"
          f"KNOWN SOURCES: {sources}"
      )
        completion = client.chat.completions.create(
            model=settings.model,
            max_tokens=1024,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},   # works on llama-swap; json_schema does not
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = completion.choices[0].message.content or "{}"
        # Validate/coerce the model's JSON into our typed schema.
        result = Diagnosis.model_validate_json(raw)
        # Make sure the cited sources include what we actually retrieved.
        if not result.sources:
            result.sources = sources
        return {"result": result}
    
    def should_continue(state: AgentState) -> str:
        """Decide the next node after diagnose.

        Loop back to `retrieve` only if the LLM asked for more AND we're still
        under the maxSteps bound. Otherwise proceed to `summarize`.
        """
        if state.get("needs_more") and state.get("steps", 0) < settings.max_steps:
            return "gather_signals"
        return "summarize"

     # ---- Assemble the graph --------------------------------------------
    graph = StateGraph(AgentState)
    graph.add_node("classify", classify)
    graph.add_node("retrieve", retrieve)
    graph.add_node("diagnose", diagnose)
    graph.add_node("gather_signals", gather_signals)
    graph.add_node("summarize", summarize)

    graph.add_edge(START, "classify")     # entry
    graph.add_edge("classify", "retrieve")
    graph.add_edge("retrieve", "diagnose")
    # After diagnose, the target is chosen at runtime by should_continue:
    graph.add_conditional_edges(
        "diagnose", should_continue,
        {"gather_signals": "gather_signals", "summarize": "summarize"},
    )
    graph.add_edge("gather_signals", "diagnose")
    graph.add_edge("summarize", END)      # exit

    return graph.compile()