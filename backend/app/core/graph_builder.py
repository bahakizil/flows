from __future__ import annotations

"""GraphBuilder: Build LangGraph execution graphs from flow definitions.

This replaces the previous DynamicChainBuilder implementation and brings
full LangGraph features: conditional routing, loop/parallel constructs,
checkpointer support, streaming execution, and credential context
handling so that nodes can retrieve per-user credentials securely.
"""

from typing import Dict, Any, List, Optional, Callable, Type, Union, AsyncGenerator
from dataclasses import dataclass
from enum import Enum
import uuid
import asyncio
import os

from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.runnables import Runnable, RunnableConfig

from app.core.state import FlowState
from app.nodes.base import BaseNode
from app.core.checkpointer import get_postgres_checkpointer
from app.core.credential_provider import credential_provider

__all__ = ["GraphBuilder", "NodeConnection", "GraphNodeInstance", "ControlFlowType"]


@dataclass
class NodeConnection:
    """Represents a connection (edge) between two nodes in the UI."""

    source_node_id: str
    source_handle: str
    target_node_id: str
    target_handle: str
    data_type: str = "any"


@dataclass
class GraphNodeInstance:
    """A concrete node instance ready to execute inside LangGraph."""

    id: str
    type: str
    node_instance: BaseNode
    metadata: Dict[str, Any]
    user_data: Dict[str, Any]


class ControlFlowType(str, Enum):
    SEQUENTIAL = "sequential"
    CONDITIONAL = "conditional"
    LOOP = "loop"
    PARALLEL = "parallel"


class GraphBuilder:
    """Convert ReactFlow JSON into an executable LangGraph pipeline."""

    def __init__(self, node_registry: Dict[str, Type[BaseNode]], checkpointer=None):
        self.node_registry = node_registry
        # Pick the best available checkpointer
        self.checkpointer = checkpointer or self._get_checkpointer()
        # State that is rebuilt on every `build_from_flow`
        self.nodes: Dict[str, GraphNodeInstance] = {}
        self.connections: List[NodeConnection] = []
        self.control_flow_nodes: Dict[str, Dict[str, Any]] = {}
        self.graph: Optional[CompiledStateGraph] = None

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def build_from_flow(self, flow_data: Dict[str, Any], user_id: Optional[str] = None) -> CompiledStateGraph:
        """Given the JSON sent from the frontend, construct LangGraph."""
        nodes = flow_data.get("nodes", [])
        edges = flow_data.get("edges", [])

        # Reset builder state
        self.nodes.clear()
        self.connections.clear()
        self.control_flow_nodes.clear()

        # Every execution gets its own credential context so that node
        # instances can resolve credentials via `credential_provider`.
        context_id = str(uuid.uuid4())
        if user_id:
            credential_provider.set_user_context(context_id, user_id)

        try:
            self._parse_connections(edges)
            self._identify_control_flow_nodes(nodes)
            self._instantiate_nodes(nodes, context_id)
            self.graph = self._build_langgraph()
            return self.graph
        finally:
            if user_id:
                credential_provider.clear_user_context(context_id)

    async def execute(
        self,
        inputs: Dict[str, Any],
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        stream: bool = False,
    ) -> Union[Dict[str, Any], AsyncGenerator[Dict[str, Any], None]]:
        """Run the compiled graph (call `build_from_flow` first)."""
        if not self.graph:
            raise ValueError("Graph has not been built. Call build_from_flow().")

        # Prepare initial FlowState
        init_state = FlowState(
            current_input=inputs.get("input", ""),
            session_id=session_id or str(uuid.uuid4()),
            user_id=user_id,
            workflow_id=workflow_id,
            variables=inputs,
        )
        config: RunnableConfig = {"configurable": {"thread_id": init_state.session_id}}

        return (
            self._execute_stream(init_state, config)
            if stream
            else await self._execute_sync(init_state, config)
        )

    # ------------------------------------------------------------------
    # Internal helpers – build phase
    # ------------------------------------------------------------------
    def _get_checkpointer(self):
        if os.getenv("DISABLE_DATABASE", "false").lower() == "true":
            # Use NoOpCheckpointer to avoid msgpack serialization issues in standalone mode
            from app.core.checkpointer import NoOpCheckpointer
            return NoOpCheckpointer()

        try:
            pg_cp = get_postgres_checkpointer()
            if pg_cp and pg_cp.is_available():
                return pg_cp
        except Exception:
            pass
        return MemorySaver()

    def _parse_connections(self, edges: List[Dict[str, Any]]):
        for edge in edges:
            self.connections.append(
                NodeConnection(
                    source_node_id=edge["source"],
                    source_handle=edge.get("sourceHandle", "output"),
                    target_node_id=edge["target"],
                    target_handle=edge.get("targetHandle", "input"),
                )
            )

    def _identify_control_flow_nodes(self, nodes: List[Dict[str, Any]]):
        """Detect Condition/Loop/Parallel helper nodes by naming convention."""
        for node in nodes:
            node_type = node.get("type", "").lower()
            node_id = node["id"]
            # Treat dedicated flow-control helper nodes specially, but skip
            # generic `ConditionalChain` which is a regular processing node.
            if ("condition" in node_type or "router" in node_type) and node_type != "conditionalchain":
                self.control_flow_nodes[node_id] = {
                    "type": ControlFlowType.CONDITIONAL,
                    "data": node.get("data", {}),
                }
            elif "loop" in node_type:
                self.control_flow_nodes[node_id] = {
                    "type": ControlFlowType.LOOP,
                    "data": node.get("data", {}),
                }
            elif "parallel" in node_type:
                self.control_flow_nodes[node_id] = {
                    "type": ControlFlowType.PARALLEL,
                    "data": node.get("data", {}),
                }

    def _instantiate_nodes(self, nodes: List[Dict[str, Any]], context_id: str):
        """Instantiate nodes and build proper connection mappings with source handle support."""
        for node_def in nodes:
            node_id = node_def["id"]
            node_type = node_def["type"]
            user_data = node_def.get("data", {})

            if node_id in self.control_flow_nodes:
                continue  # Skip – handled separately

            node_cls = self.node_registry.get(node_type)
            if not node_cls:
                raise ValueError(f"Unknown node type: {node_type}")

            instance = node_cls()
            instance.node_id = node_id
            instance.context_id = context_id

            # 🔥 NEW: Build comprehensive connection mapping
            # Format: {target_handle: {"source_node_id": str, "source_handle": str}}
            input_connections = {}
            output_connections = {}  # Track what this node outputs to
            
            # Find all connections targeting this node
            for conn in self.connections:
                if conn.target_node_id == node_id:
                    input_connections[conn.target_handle] = {
                        "source_node_id": conn.source_node_id,
                        "source_handle": conn.source_handle
                    }
                    print(f"[DEBUG] Mapping input '{conn.target_handle}' -> source node '{conn.source_node_id}' handle '{conn.source_handle}'")
                
                if conn.source_node_id == node_id:
                    if conn.source_handle not in output_connections:
                        output_connections[conn.source_handle] = []
                    output_connections[conn.source_handle].append({
                        "target_node_id": conn.target_node_id,
                        "target_handle": conn.target_handle
                    })

            # 🔥 CRITICAL: Set connection mappings on the node instance
            instance._input_connections = input_connections
            instance._output_connections = output_connections
            
            # Store user configuration from frontend
            instance.user_data = user_data

            # Create GraphNodeInstance
            self.nodes[node_id] = GraphNodeInstance(
                id=node_id,
                type=node_type,
                node_instance=instance,
                metadata={},
                user_data=user_data,
            )
            
            print(f"[DEBUG] ✅ Instantiated node '{node_id}' with {len(input_connections)} inputs, {len(output_connections)} outputs")

    # ------------------------------------------------------------------
    # Internal – Graph building
    # ------------------------------------------------------------------
    def _build_langgraph(self) -> CompiledStateGraph:
        graph = StateGraph(FlowState)

        # 1) Regular nodes
        for node_id, n in self.nodes.items():
            graph.add_node(node_id, self._wrap_node(node_id, n))

        # 2) Control-flow constructs
        self._add_control_flow_edges(graph)

        # 3) Regular edges
        self._add_regular_edges(graph)

        # 4) START & END
        self._add_start_end_connections(graph)

        return graph.compile(checkpointer=self.checkpointer)

    def _wrap_node(self, node_id: str, gnode: GraphNodeInstance) -> Callable[[FlowState], Dict[str, Any]]:
        node_func = gnode.node_instance.to_graph_node()

        def wrapper(state: FlowState) -> Dict[str, Any]:  # noqa: D401
            """Wrapper that injects static vars then calls node_func."""
            for k, v in gnode.user_data.items():
                state.set_variable(k, v)
            return node_func(state)

        wrapper.__name__ = f"node_{node_id}"
        return wrapper

    # ---------------- Control flow helpers -----------------
    def _add_control_flow_edges(self, graph: StateGraph):
        for node_id, info in self.control_flow_nodes.items():
            ctype: ControlFlowType = info["type"]  # type: ignore[arg-type]
            cdata = info["data"]
            if ctype == ControlFlowType.CONDITIONAL:
                self._add_conditional_routing(graph, node_id, cdata)
            elif ctype == ControlFlowType.LOOP:
                self._add_loop_logic(graph, node_id, cdata)
            elif ctype == ControlFlowType.PARALLEL:
                self._add_parallel_fanout(graph, node_id, cdata)

    def _add_conditional_routing(self, graph: StateGraph, node_id: str, cfg: Dict[str, Any]):
        outgoing = [c for c in self.connections if c.source_node_id == node_id]
        if len(outgoing) < 2:
            return

        cond_field = cfg.get("condition_field", "last_output")
        cond_type = cfg.get("condition_type", "contains")

        def route(state: FlowState) -> str:
            value = state.get_variable(cond_field, state.last_output)
            for conn in outgoing:
                branch_cfg = cfg.get(f"branch_{conn.target_node_id}", {})
                if self._evaluate_condition(value, branch_cfg, cond_type):
                    return conn.target_node_id
            return outgoing[0].target_node_id  # default

        graph.add_node(node_id, lambda s: s)  # dummy pass-through
        graph.add_conditional_edges(
            node_id,
            route,
            {c.target_node_id: c.target_node_id for c in outgoing},
        )

    def _add_loop_logic(self, graph: StateGraph, node_id: str, cfg: Dict[str, Any]):
        """Create a simple counter-based loop helper.

        Flow: LOOP_NODE  ─▶ BODY_NODE … ─▶ LOOP_NODE (edge in original JSON)
              ^                                           |
              └──────────────────────── < max_iter ? -----┘
        The JSON must contain at least one outgoing edge from the loop helper
        designating the *body* node.  After each body execution, control should
        return to the loop helper (another edge).  This function injects the
        conditional routing logic so that execution either jumps to the BODY
        or terminates (END).
        """
        max_iter = int(cfg.get("max_iterations", 3))

        # Identify body (first outgoing edge)
        outgoing = [c for c in self.connections if c.source_node_id == node_id]
        if not outgoing:
            return
        body_id = outgoing[0].target_node_id

        def loop_router(state: FlowState) -> str:
            counter = state.get_variable(f"{node_id}_counter", 0)
            if counter >= max_iter:
                return END
            state.set_variable(f"{node_id}_counter", counter + 1)
            return body_id

        graph.add_node(node_id, lambda s: s)  # condition node (no-op)
        graph.add_conditional_edges(node_id, loop_router, {body_id: body_id, END: END})

    def _add_parallel_fanout(self, graph: StateGraph, node_id: str, cfg: Dict[str, Any]):
        """Add a fan-out node that duplicates state to multiple branches."""
        outgoing = [c for c in self.connections if c.source_node_id == node_id]
        if not outgoing:
            return

        branch_ids = [c.target_node_id for c in outgoing]

        def fan_out(state: FlowState):  # noqa: D401
            # Return mapping of channel -> state to create parallel branches
            return {bid: state.copy() for bid in branch_ids}

        graph.add_node(node_id, fan_out)
        for bid in branch_ids:
            graph.add_edge(node_id, bid)

    def _evaluate_condition(self, value: Any, branch_cfg: Dict[str, Any], cond_type: str) -> bool:
        try:
            if cond_type == "contains":
                return str(branch_cfg.get("value", "")) in str(value)
            if cond_type == "equals":
                return str(value) == branch_cfg.get("value", "")
            if cond_type == "greater_than":
                return float(value) > float(branch_cfg.get("value", 0))
            if cond_type == "custom":
                return bool(eval(branch_cfg.get("expression", "False"), {"value": value}))
        except Exception:
            return False
        return False

    # ---------------- Regular edges & START/END ------------
    def _add_regular_edges(self, graph: StateGraph):
        print(f"[DEBUG] Adding regular edges:")
        
        # Group connections by target node to handle multi-input nodes properly
        target_groups = {}
        for c in self.connections:
            if c.source_node_id in self.control_flow_nodes:
                continue  # handled by control-flow
            if c.target_node_id not in target_groups:
                target_groups[c.target_node_id] = []
            target_groups[c.target_node_id].append(c.source_node_id)
        
        # Add edges, ensuring proper dependency order
        for target_node, source_nodes in target_groups.items():
            print(f"[DEBUG] Target {target_node} depends on: {source_nodes}")
            for source_node in source_nodes:
                print(f"[DEBUG]   {source_node} -> {target_node}")
                graph.add_edge(source_node, target_node)

    def _add_start_end_connections(self, graph: StateGraph):
        incoming_targets = {c.target_node_id for c in self.connections}
        start_nodes = [nid for nid in list(self.nodes) + list(self.control_flow_nodes) if nid not in incoming_targets]
        if not start_nodes:
            start_nodes = [list(self.nodes.keys())[0]] if self.nodes else []
        for n in start_nodes:
            graph.add_edge(START, n)

        outgoing_sources = {c.source_node_id for c in self.connections}
        end_nodes = [nid for nid in self.nodes if nid not in outgoing_sources and nid not in self.control_flow_nodes]
        for n in end_nodes:
            graph.add_edge(n, END)

    # ------------------------------------------------------------------
    # Internal – Execution helpers
    # ------------------------------------------------------------------
    async def _execute_sync(self, init_state: FlowState, config: RunnableConfig) -> Dict[str, Any]:
        try:
            # Prefer async interface if implemented
            result_state = await self.graph.ainvoke(init_state, config=config)  # type: ignore[arg-type]
            return {
                "success": True,
                "result": getattr(result_state, "last_output", str(result_state)),
                "state": getattr(result_state, "model_dump", lambda: result_state)(),
                "executed_nodes": getattr(result_state, "executed_nodes", []),
                "session_id": getattr(result_state, "session_id", init_state.session_id),
            }
        except NotImplementedError:
            # Fallback to sync invoke in thread pool to avoid blocking
            import asyncio, functools
            loop = asyncio.get_event_loop()
            result_state = await loop.run_in_executor(
                None, functools.partial(self.graph.invoke, init_state, config=config)  # type: ignore[arg-type]
            )
            return {
                "success": True,
                "result": getattr(result_state, "last_output", str(result_state)),
                "state": getattr(result_state, "model_dump", lambda: result_state)(),
                "executed_nodes": getattr(result_state, "executed_nodes", []),
                "session_id": getattr(result_state, "session_id", init_state.session_id),
            }
        except Exception as e:
            return {"success": False, "error": str(e), "error_type": type(e).__name__, "session_id": init_state.session_id}

    async def _execute_stream(self, init_state: FlowState, config: RunnableConfig):
        try:
            yield {"type": "start", "session_id": init_state.session_id, "message": "Starting workflow execution"}
            async for ev in self.graph.astream_events(init_state, config=config):  # type: ignore[arg-type]
                ev_type = ev.get("event", "")
                if ev_type == "on_chain_start":
                    yield {"type": "node_start", "node_id": ev.get("name", "unknown"), "metadata": ev.get("data", {})}
                elif ev_type == "on_chain_end":
                    yield {"type": "node_end", "node_id": ev.get("name", "unknown"), "output": ev.get("data", {}).get("output")}
                elif ev_type == "on_llm_new_token":
                    yield {"type": "token", "content": ev.get("data", {}).get("chunk", "")}
                elif ev_type == "on_chain_error":
                    yield {"type": "error", "error": str(ev.get("data", {}).get("error", "Unknown error"))}
            final_state = await self.graph.aget_state(config)  # type: ignore[arg-type]
            yield {
                "type": "complete",
                "result": getattr(final_state.values, "last_output", ""),
                "executed_nodes": getattr(final_state.values, "executed_nodes", []),
                "session_id": init_state.session_id,
            }
        except Exception as e:
            yield {"type": "error", "error": str(e), "error_type": type(e).__name__} 