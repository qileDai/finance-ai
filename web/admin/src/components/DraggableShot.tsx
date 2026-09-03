import { useEffect, useRef, useState, type MouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { Button, Image, Space } from "antd";
import { JOB_SHOT_PREVIEW } from "./ui";
import {
  downloadBlob,
  fetchShotBlob,
  isDirectoryPickerSupported,
  writePng,
  type ShotDirHandle,
} from "../lib/saveFolder";

export type JobShotType = "esubmit" | "success";

type Props = {
  src: string;
  filename: string;
  width?: number;
  height?: number;
  dir: ShotDirHandle | null;
  zoneEl: HTMLElement | null;
  ensureFolder: () => Promise<ShotDirHandle | null>;
  onSaved: (filename: string) => void;
  onError: (message: string) => void;
  onHoverZone: (hover: boolean) => void;
};

export function jobShotFilename(jobId: number, type: JobShotType): string {
  return `job-${jobId}-${type}.png`;
}

const DRAG_PX = 8;

function overZone(x: number, y: number, zone: HTMLElement | null): boolean {
  if (!zone) return false;
  const r = zone.getBoundingClientRect();
  return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
}

export function DraggableShot({
  src,
  filename,
  width = 80,
  height = 50,
  dir,
  zoneEl,
  ensureFolder,
  onSaved,
  onError,
  onHoverZone,
}: Props) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ghost, setGhost] = useState<{ x: number; y: number } | null>(null);
  const skipPreviewRef = useRef(false);
  const draggingRef = useRef(false);
  const startRef = useRef<{ x: number; y: number } | null>(null);
  const dirRef = useRef(dir);
  dirRef.current = dir;
  const zoneRef = useRef(zoneEl);
  zoneRef.current = zoneEl;

  useEffect(() => {
    return () => onHoverZone(false);
  }, [onHoverZone]);

  async function saveToDir(target: ShotDirHandle) {
    const blob = await fetchShotBlob(src);
    await writePng(target, filename, blob);
    onSaved(filename);
  }

  async function saveAsDownload() {
    const blob = await fetchShotBlob(src);
    downloadBlob(filename, blob);
    onSaved(filename);
  }

  async function onSaveClick(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    setSaving(true);
    try {
      if (!isDirectoryPickerSupported()) {
        await saveAsDownload();
        return;
      }
      const handle = dirRef.current || (await ensureFolder());
      if (!handle) return;
      await saveToDir(handle);
    } catch (err) {
      onError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  function endPointer(e: PointerEvent) {
    const wasDragging = draggingRef.current;
    const hit = overZone(e.clientX, e.clientY, zoneRef.current);
    draggingRef.current = false;
    startRef.current = null;
    setGhost(null);
    onHoverZone(false);
    window.removeEventListener("pointermove", onWindowMove);
    window.removeEventListener("pointerup", endPointer);
    window.removeEventListener("pointercancel", endPointer);
    if (!wasDragging) return;
    skipPreviewRef.current = true;
    if (!hit) return;
    void (async () => {
      try {
        const handle = dirRef.current || (await ensureFolder());
        if (!handle) return;
        await saveToDir(handle);
      } catch (err) {
        onError(err instanceof Error ? err.message : "保存失败");
      }
    })();
  }

  function onWindowMove(e: PointerEvent) {
    const start = startRef.current;
    if (!start) return;
    const dist = Math.hypot(e.clientX - start.x, e.clientY - start.y);
    if (!draggingRef.current && dist >= DRAG_PX) {
      draggingRef.current = true;
      skipPreviewRef.current = true;
    }
    if (!draggingRef.current) return;
    setGhost({ x: e.clientX, y: e.clientY });
    onHoverZone(overZone(e.clientX, e.clientY, zoneRef.current));
  }

  function onPointerDown(e: ReactPointerEvent) {
    if (e.button !== 0) return;
    startRef.current = { x: e.clientX, y: e.clientY };
    draggingRef.current = false;
    window.addEventListener("pointermove", onWindowMove);
    window.addEventListener("pointerup", endPointer);
    window.addEventListener("pointercancel", endPointer);
  }

  return (
    <Space size={4} align="center" onClick={(e) => e.stopPropagation()}>
      <div
        className="draggable-shot"
        title="拖到下方保存区，或点保存"
        onPointerDown={onPointerDown}
      >
        <Image
          src={src}
          width={width}
          height={height}
          style={{ objectFit: "cover", cursor: "pointer" }}
          preview={{
            ...JOB_SHOT_PREVIEW,
            open: previewOpen,
            onOpenChange: (open) => {
              if (open && skipPreviewRef.current) {
                skipPreviewRef.current = false;
                return;
              }
              skipPreviewRef.current = false;
              // Portal close clicks bubble to Image onPreview; ignore that reopen.
              if (open && previewOpen) return;
              setPreviewOpen(open);
            },
          }}
        />
      </div>
      <Button size="small" loading={saving} onClick={onSaveClick}>
        保存
      </Button>
      {ghost ? (
        <img
          className="shot-save-ghost"
          src={src}
          alt=""
          style={{ left: ghost.x, top: ghost.y }}
        />
      ) : null}
    </Space>
  );
}
