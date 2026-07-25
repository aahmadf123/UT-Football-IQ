"use client";

/**
 * Shared film-upload widget (previously duplicated between the page shell and
 * the Film Room hub).
 *
 * `useUploadWidget()` returns:
 *   - `openFilePicker` — hand this to any "Upload Film" button
 *   - `widget`         — render once near the top of the page; it contains the
 *                        hidden `<input type="file">` and the status notice
 *
 * Uploads flow through `useAppState().addUploads` (Worker upload-url → R2 PUT
 * → backend register), so this widget is purely the trigger + feedback shell.
 */

import { useRef, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { Alert, AlertDescription } from "@/components/ui/alert";

export function useUploadWidget(options?: {
  successMessage?: (count: number) => string;
}): { openFilePicker: () => void; widget: React.ReactNode } {
  const { addUploads } = useAppState();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadStatus, setUploadStatus] = useState<{ text: string; failed: boolean } | null>(null);

  const openFilePicker = () => fileInputRef.current?.click();

  const handleFileChange = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) return;
    try {
      const created = await addUploads(files);
      setUploadStatus({
        text:
          options?.successMessage?.(created.length) ??
          `Uploaded ${created.length} clip${created.length === 1 ? "" : "s"} — track processing in Film Room → Upload / Process Film.`,
        failed: false,
      });
      setTimeout(() => setUploadStatus(null), 5000);
    } catch (err) {
      setUploadStatus({
        text: `Upload failed: ${err instanceof Error ? err.message : String(err)}`,
        failed: true,
      });
    } finally {
      // Reset input so the same file can be re-selected.
      event.target.value = "";
    }
  };

  const widget = (
    <>
      <input
        type="file"
        ref={fileInputRef}
        onChange={handleFileChange}
        accept="video/*"
        multiple
        className="hidden"
      />
      {uploadStatus && (
        <Alert
          variant={uploadStatus.failed ? "destructive" : "default"}
          role="status"
          data-testid="upload-toast"
          className={
            uploadStatus.failed
              ? "mb-3"
              : "mb-3 border-status-ok/40 bg-status-ok/10 text-status-ok [&>*]:text-status-ok"
          }
        >
          <AlertDescription className={uploadStatus.failed ? "" : "text-status-ok"}>
            {uploadStatus.text}
          </AlertDescription>
        </Alert>
      )}
    </>
  );

  return { openFilePicker, widget };
}
