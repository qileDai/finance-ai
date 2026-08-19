import { useEffect, useRef, useState } from "react";

type Props = {
  src: string;
  onConfirm: (file: File) => void;
  onCancel: () => void;
};

type Rect = { x: number; y: number; w: number; h: number };

type ImageLayout = {
  left: number;
  top: number;
  width: number;
  height: number;
  scale: number;
  naturalWidth: number;
  naturalHeight: number;
};

const MIN_SIZE = 16;

function getImageLayout(img: HTMLImageElement | null): ImageLayout | null {
  if (!img || !img.naturalWidth || !img.naturalHeight) return null;
  const br = img.getBoundingClientRect();
  const scale = Math.min(br.width / img.naturalWidth, br.height / img.naturalHeight);
  const width = img.naturalWidth * scale;
  const height = img.naturalHeight * scale;
  return {
    left: br.left + (br.width - width) / 2,
    top: br.top + (br.height - height) / 2,
    width,
    height,
    scale,
    naturalWidth: img.naturalWidth,
    naturalHeight: img.naturalHeight,
  };
}

function clampPoint(x: number, y: number, layout: ImageLayout) {
  return {
    x: Math.min(Math.max(x, layout.left), layout.left + layout.width),
    y: Math.min(Math.max(y, layout.top), layout.top + layout.height),
  };
}

function normRect(a: { x: number; y: number }, b: { x: number; y: number }): Rect {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return { x, y, w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) };
}

export function ScreenshotCrop({ src, onConfirm, onCancel }: Props) {
  const imgRef = useRef<HTMLImageElement>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);
  const [sel, setSel] = useState<Rect | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [, setTick] = useState(0);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
      if (e.key === "Enter") {
        e.preventDefault();
        void confirmCrop();
      }
    };
    const onResize = () => setTick((n) => n + 1);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    };
  });

  const layout = getImageLayout(imgRef.current);

  function confirmCrop() {
    const img = imgRef.current;
    const box = sel;
    const lay = getImageLayout(img);
    if (!img || !box || !lay || box.w < MIN_SIZE || box.h < MIN_SIZE || busy) return;
    setBusy(true);
    const sx = Math.max(0, (box.x - lay.left) / lay.scale);
    const sy = Math.max(0, (box.y - lay.top) / lay.scale);
    const sw = Math.min(lay.naturalWidth - sx, box.w / lay.scale);
    const sh = Math.min(lay.naturalHeight - sy, box.h / lay.scale);
    const cw = Math.max(1, Math.round(sw));
    const ch = Math.max(1, Math.round(sh));
    const canvas = document.createElement("canvas");
    canvas.width = cw;
    canvas.height = ch;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      setBusy(false);
      return;
    }
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, cw, ch);
    canvas.toBlob((blob) => {
      if (!blob) {
        setBusy(false);
        return;
      }
      onConfirm(new File([blob], `screenshot-${Date.now()}.png`, { type: "image/png" }));
    }, "image/png");
  }

  function onPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest(".ss-crop-toolbar")) return;
    const lay = getImageLayout(imgRef.current);
    if (!lay) return;
    const p = clampPoint(e.clientX, e.clientY, lay);
    dragStart.current = p;
    setDragging(true);
    setSel({ x: p.x, y: p.y, w: 0, h: 0 });
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!dragging || !dragStart.current) return;
    const lay = getImageLayout(imgRef.current);
    if (!lay) return;
    setSel(normRect(dragStart.current, clampPoint(e.clientX, e.clientY, lay)));
  }

  function onPointerUp() {
    if (!dragging) return;
    setDragging(false);
    dragStart.current = null;
    setSel((s) => (s && s.w >= MIN_SIZE && s.h >= MIN_SIZE ? s : null));
  }

  const shades = layout
    ? sel && (sel.w > 0 || sel.h > 0)
      ? [
          {
            left: layout.left,
            top: layout.top,
            width: layout.width,
            height: Math.max(0, sel.y - layout.top),
          },
          {
            left: layout.left,
            top: sel.y,
            width: Math.max(0, sel.x - layout.left),
            height: sel.h,
          },
          {
            left: sel.x + sel.w,
            top: sel.y,
            width: Math.max(0, layout.left + layout.width - (sel.x + sel.w)),
            height: sel.h,
          },
          {
            left: layout.left,
            top: sel.y + sel.h,
            width: layout.width,
            height: Math.max(0, layout.top + layout.height - (sel.y + sel.h)),
          },
        ]
      : [
          {
            left: layout.left,
            top: layout.top,
            width: layout.width,
            height: layout.height,
          },
        ]
    : [];

  const showToolbar = Boolean(sel && !dragging && sel.w >= MIN_SIZE && sel.h >= MIN_SIZE);
  let toolbarStyle: React.CSSProperties = {};
  if (showToolbar && sel) {
    const left = Math.min(sel.x + sel.w - 148, window.innerWidth - 160);
    const top =
      sel.y + sel.h + 8 + 36 < window.innerHeight ? sel.y + sel.h + 8 : Math.max(8, sel.y - 40);
    toolbarStyle = { left: Math.max(8, left), top };
  }

  return (
    <div
      className="ss-crop"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    >
      <img
        ref={imgRef}
        className="ss-crop-img"
        src={src}
        alt=""
        draggable={false}
        onLoad={() => setTick((n) => n + 1)}
      />
      {shades.map((s, i) => (
        <div
          key={i}
          className="ss-crop-shade"
          style={{ left: s.left, top: s.top, width: s.width, height: s.height }}
        />
      ))}
      {sel && sel.w > 0 && sel.h > 0 ? (
        <div
          className="ss-crop-box"
          style={{ left: sel.x, top: sel.y, width: sel.w, height: sel.h }}
        />
      ) : null}
      <div className="ss-crop-hint">拖动鼠标框选证件区域 · Enter 完成 · Esc 取消</div>
      {showToolbar ? (
        <div
          className="ss-crop-toolbar"
          style={toolbarStyle}
          onPointerDown={(e) => e.stopPropagation()}
        >
          <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => confirmCrop()}
            disabled={busy}
          >
            {busy ? "处理中…" : "完成"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
