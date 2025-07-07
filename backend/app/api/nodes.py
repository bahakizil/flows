from fastapi import APIRouter, HTTPException
from typing import List, Optional
from app.core.node_registry import node_registry
from app.nodes.base import NodeMetadata
from app.models.node import NodeCategory
from pydantic import BaseModel

router = APIRouter()

class CustomNodeCreate(BaseModel):
    name: str
    description: str
    category: NodeCategory
    config: dict
    code: str
    is_public: bool = False

class CustomNodeResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: str
    category: str
    config: dict
    code: str
    is_public: bool
    created_at: str
    updated_at: Optional[str] = None

# Legacy Supabase database has been removed; custom-node persistence temporarily disabled.

NOT_IMPLEMENTED_NODES = HTTPException(
    status_code=501,
    detail="Custom node persistence is temporarily disabled while migrating to SQLAlchemy layer."
)

@router.get("/", response_model=List[NodeMetadata])
async def list_nodes(
    category: Optional[NodeCategory] = None,
):
    """List all available nodes"""
    if category is not None:
        # Convert Enum to its value (string) so comparison with metadata works
        category_name = category.value if hasattr(category, "value") else str(category)
        nodes = node_registry.get_nodes_by_category(category_name)
    else:
        nodes = node_registry.get_all_nodes()
    
    return nodes

@router.get("/categories")
async def list_categories():
    """List all node categories"""
    return [
        {
            "name": category.value,
            "display_name": category.value.replace("_", " ").title(),
            "icon": get_category_icon(category)
        }
        for category in NodeCategory
    ]

@router.post("/custom", response_model=CustomNodeResponse)
async def create_custom_node_stub(*_, **__):  # noqa: ANN001
    """Create a custom node (stub)"""
    raise NOT_IMPLEMENTED_NODES

@router.get("/custom", response_model=List[CustomNodeResponse])
async def list_custom_nodes_stub(*_, **__):  # noqa: ANN001
    """List custom nodes (stub)"""
    raise NOT_IMPLEMENTED_NODES

@router.get("/custom/{node_id}", response_model=CustomNodeResponse)
async def get_custom_node_stub(*_, **__):  # noqa: ANN001
    """Get a custom node by ID (stub)"""
    raise NOT_IMPLEMENTED_NODES

@router.delete("/custom/{node_id}")
async def delete_custom_node_stub(*_, **__):  # noqa: ANN001
    """Delete a custom node (stub)"""
    raise NOT_IMPLEMENTED_NODES

def get_category_icon(category: NodeCategory) -> str:
    """Get icon for node category"""
    icons = {
        NodeCategory.LLM: "🧠",
        NodeCategory.TOOL: "🔧",
        NodeCategory.AGENT: "🤖",
        NodeCategory.CHAIN: "⛓️",
        NodeCategory.MEMORY: "💾",
        NodeCategory.VECTOR_STORE: "📊",
        NodeCategory.DOCUMENT_LOADER: "📄",
        NodeCategory.TEXT_SPLITTER: "✂️",
        NodeCategory.EMBEDDING: "🔢",
        NodeCategory.UTILITY: "⚙️",
        NodeCategory.INTEGRATION: "🔗",
        NodeCategory.CUSTOM: "🎨"
    }
    return icons.get(category, "📋") 