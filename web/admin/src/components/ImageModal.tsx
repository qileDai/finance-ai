import { useEffect, useState } from "react";

type Props = {
  src: string;
  alt?: string;
  open: boolean;
  onClose: () => void;
};

export function ImageModal({ src, alt = "", open, onClose }: Props) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  useEffect(() => {
    if (!open) return;
    setScale(1);
    setOffset({ x: 0, y: 0 });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    setScale((s) => Math.max(0.5, Math.min(5, s - e.deltaY * 0.001)));
  };

  const onMouseDown = (e: React.MouseEvent) => {
    setDragging(true);
    setDragStart({ x: e.clientX - offset.x, y: e.clientY - offset.y });
  };

  const onMouseMove = (e: React.MouseEvent) => {
    if (!dragging) return;
    setOffset({ x: e.clientX - dragStart.x, y: e.clientY - dragStart.y });
  };

  const onMouseUp = () => setDragging(false);

  // 点击 overlay 空白处（非图片、非 toolbar）关闭浮层
  const onOverlayMouseDown = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) onClose();
  };

  return (
    <div className="img-modal-overlay" onMouseDown={onOverlayMouseDown}>
      <div className="img-modal-toolbar">
        <button type="button" onClick={() => setScale((s) => Math.min(5, s + 0.2))}>
          +
        </button>
        <span>{(scale * 100).toFixed(0)}%</span>
        <button type="button" onClick={() => setScale((s) => Math.max(0.5, s - 0.2))}>
          -
        </button>
        <button
          type="button"
          onClick={() => {
            setScale(1);
            setOffset({ x: 0, y: 0 });
          }}
        >
          重置
        </button>
        <button type="button" onClick={onClose}>
          ✕
        </button>
      </div>
      <div
        className="img-modal-body"
        onWheel={onWheel}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
      >
        <img
          src={src}
          alt={alt}
          style={{
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
            cursor: dragging ? "grabbing" : "grab",
          }}
          draggable={false}
        />
      </div>
    </div>
  );
}
