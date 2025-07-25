import React, { useRef, useState } from "react";
import { useReactFlow, Handle, Position } from "@xyflow/react";
import { Trash } from "lucide-react";
import InMemoryCacheConfigModal from "~/components/modals/cache/InMemoryCacheModal";
import { MemoryStick } from "lucide-react";

interface InMemoryCacheNodeProps {
  data: any;
  id: string;
}

function InMemoryCacheNode({ data, id }: InMemoryCacheNodeProps) {
  const { setNodes } = useReactFlow();
  const [isHovered, setIsHovered] = useState(false);
  const modalRef = useRef<HTMLDialogElement>(null);

  const handleOpenModal = () => {
    modalRef.current?.showModal();
  };

  const handleConfigSave = (newConfig: any) => {
    setNodes((nodes: any[]) =>
      nodes.map((node) =>
        node.id === id
          ? { ...node, data: { ...node.data, ...newConfig } }
          : node
      )
    );
  };
  const handleDeleteNode = (e: React.MouseEvent) => {
    e.stopPropagation();
    setNodes((nodes) => nodes.filter((node) => node.id !== id));
  };

  return (
    <>
      {/* Ana node kutusu */}
      <div
        className={`w-20 h-20 rounded-full flex items-center justify-center gap-3 px-4 py-4  border-2 text-gray-700 font-medium cursor-pointer transition-all border-gray-400 bg-gray-100 hover:bg-gray-200`}
        onDoubleClick={handleOpenModal}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        title="Çift tıklayarak konfigüre edin"
      >
        <div className=" rounded-2xl">
          <MemoryStick />
        </div>
        {isHovered && (
          <button
            className="absolute -top-2 -right-2 w-6 h-6 bg-red-500 hover:bg-red-600 text-white rounded-full border-2 border-white shadow-lg transition-all duration-200 hover:scale-110 flex items-center justify-center z-10"
            onClick={handleDeleteNode}
            title="Delete Node"
          >
            <Trash size={8} />
          </button>
        )}
        <div
          className="absolute text-xs text-gray-600 font-medium pointer-events-none whitespace-nowrap"
          style={{
            bottom: "-30%",
            transform: "translateY(-50%)",
          }}
        >
          {data?.displayName || data?.name || "InMemory Cache"}
        </div>

        <Handle
          type="source"
          position={Position.Top}
          id="output"
          className="w-3 h-3 border-2 border-gray-300 !bg-gray-400"
          style={{
            width: 10,
            height: 10,
          }}
        />
      </div>

      {/* DaisyUI dialog modal */}
      <InMemoryCacheConfigModal
        ref={modalRef}
        nodeData={data}
        onSave={handleConfigSave}
        nodeId={id}
      />
    </>
  );
}

export default InMemoryCacheNode;
