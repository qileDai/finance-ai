const DB_NAME = "finance-ai-shot-save";
const STORE = "kv";
const DIR_KEY = "directory";

export type ShotDirHandle = FileSystemDirectoryHandle;

export function isDirectoryPickerSupported(): boolean {
  return typeof window !== "undefined" && typeof window.showDirectoryPicker === "function";
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbGet(): Promise<ShotDirHandle | null> {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(DIR_KEY);
      req.onsuccess = () => resolve((req.result as ShotDirHandle | undefined) || null);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

async function idbSet(handle: ShotDirHandle): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(handle, DIR_KEY);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

async function idbClear(): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(DIR_KEY);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function ensureWritePermission(
  handle: ShotDirHandle,
): Promise<boolean> {
  const opts = { mode: "readwrite" as const };
  const q = await handle.queryPermission(opts);
  if (q === "granted") return true;
  const r = await handle.requestPermission(opts);
  return r === "granted";
}

export async function pickDirectory(): Promise<ShotDirHandle | null> {
  if (!isDirectoryPickerSupported()) {
    throw new Error("当前浏览器不支持选择文件夹，请使用 Chrome 或 Edge");
  }
  try {
    const handle = await window.showDirectoryPicker!({
      id: "icris-job-shots",
      mode: "readwrite",
    });
    await idbSet(handle);
    return handle;
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") return null;
    throw e;
  }
}

export async function loadStoredDirectory(): Promise<ShotDirHandle | null> {
  if (!isDirectoryPickerSupported()) return null;
  try {
    const handle = await idbGet();
    if (!handle) return null;
    const ok = await ensureWritePermission(handle);
    if (!ok) return null;
    return handle;
  } catch {
    return null;
  }
}

export async function clearStoredDirectory(): Promise<void> {
  await idbClear();
}

export function directoryName(handle: ShotDirHandle | null): string {
  return handle?.name || "";
}

export function safeFilename(name: string): string {
  const base = (name || "shot.png").split(/[/\\]/).pop() || "shot.png";
  return base.replace(/[<>:"|?*\u0000-\u001f]/g, "_") || "shot.png";
}

export async function writePng(
  dir: ShotDirHandle,
  filename: string,
  blob: Blob,
): Promise<void> {
  const ok = await ensureWritePermission(dir);
  if (!ok) throw new Error("没有该文件夹的写入权限，请重新选择");
  const file = await dir.getFileHandle(safeFilename(filename), { create: true });
  const writable = await file.createWritable();
  try {
    await writable.write(blob);
  } finally {
    await writable.close();
  }
}

export async function fetchShotBlob(src: string): Promise<Blob> {
  const res = await fetch(src, { credentials: "include" });
  if (!res.ok) throw new Error(`截图下载失败 (${res.status})`);
  return res.blob();
}
