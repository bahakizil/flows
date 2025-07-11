from ..base import ProviderNode, NodeMetadata, NodeInput, NodeType
from langchain.memory import ConversationBufferWindowMemory
from langchain_core.runnables import Runnable
from typing import cast

class ConversationMemoryNode(ProviderNode):
    def __init__(self):
        super().__init__()
        self._metadata = {
            "name": "ConversationMemory",
            "display_name": "Conversation Memory",

            "description": "Provides a conversation buffer window memory.",
            "category": "Memory",
            "node_type": NodeType.PROVIDER,
            "inputs": [
                NodeInput(name="k", type="int", description="The number of messages to keep in the buffer.", default=5),
                NodeInput(name="memory_key", type="string", description="The key for the memory in the chat history.", default="chat_history")
            ]
        }

    def execute(self, **kwargs) -> Runnable:
        """Execute with correct ProviderNode signature"""
        k = kwargs.get("k", 5)
        memory_key = kwargs.get("memory_key", "chat_history")
        
        memory = ConversationBufferWindowMemory(
            k=k,
            memory_key=memory_key,
            return_messages=True
        )
        return cast(Runnable, memory)
