/// <reference types="vite/client" />

interface Window {
  showDirectoryPicker?: (options?: {
    id?: string;
    mode?: "read" | "readwrite";
    startIn?: FileSystemHandle | string;
  }) => Promise<FileSystemDirectoryHandle>;
}
