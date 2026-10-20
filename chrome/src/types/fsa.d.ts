// File System Access API bits that TypeScript's DOM lib does not ship (Chromium-only).
interface FilePickerAcceptType {
  description?: string;
  accept: Record<string, string | string[]>;
}
interface FilePickerOptions {
  types?: FilePickerAcceptType[];
  excludeAcceptAllOption?: boolean;
  id?: string;
  startIn?: FileSystemHandle | "desktop" | "documents" | "downloads" | "music" | "pictures" | "videos";
}
interface OpenFilePickerOptions extends FilePickerOptions { multiple?: boolean }
interface SaveFilePickerOptions extends FilePickerOptions { suggestedName?: string }
interface FileSystemHandlePermissionDescriptor { mode?: "read" | "readwrite" }
interface FileSystemHandle {
  queryPermission(d?: FileSystemHandlePermissionDescriptor): Promise<PermissionState>;
  requestPermission(d?: FileSystemHandlePermissionDescriptor): Promise<PermissionState>;
}
interface DataTransferItem {
  getAsFileSystemHandle?(): Promise<FileSystemHandle | null>;
}
interface Window {
  showOpenFilePicker(o?: OpenFilePickerOptions): Promise<FileSystemFileHandle[]>;
  showSaveFilePicker(o?: SaveFilePickerOptions): Promise<FileSystemFileHandle>;
}
