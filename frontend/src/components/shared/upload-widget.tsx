"use client";

/**
 * Shared film-upload widget.
 *
 * `useUploadWidget()` returns:
 *   - `openFilePicker` — hand this to any "Upload Film" button
 *   - `widget`         — render once near the top of the page; it holds the
 *                        upload dialog and the status notice
 *
 * The button used to open a bare `<input type="file">` and fire the batch
 * straight at `addUploads(files)` with no second argument. `addUploads` has
 * always accepted session metadata and forwarded it to `POST /videos`, so that
 * missing argument is what left every video with a null `session_kind`,
 * `opponent_team` and `recorded_at` — and the Library filters and Opponent Prep
 * tab with nothing they could ever match. It now opens `UploadDialog`, which
 * collects that metadata once for the batch.
 */

import { useCallback, useMemo, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { UploadDialog } from "@/components/shared/upload-dialog";
import { draftToMetadata, type UploadMetadataDraft } from "@/lib/upload-metadata";

export function useUploadWidget(options?: {
  successMessage?: (count: number) => string;
}): { openFilePicker: () => void; widget: React.ReactNode } {
  const { addUploads, data } = useAppState();
  const [open, setOpen] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<{ text: string; failed: boolean } | null>(null);

  const openFilePicker = useCallback(() => setOpen(true), []);

  /**
   * Opponents already on file, offered as suggestions so the same team is not
   * entered three different ways — the backend groups Opponent Prep by the
   * literal string.
   */
  const opponentSuggestions = useMemo(() => {
    const names = new Set<string>();
    for (const video of data.videos ?? []) {
      const team = video.opponent_team?.trim();
      if (team) names.add(team);
    }
    return [...names].sort();
  }, [data.videos]);

  const handleSubmit = useCallback(
    async (files: File[], draft: UploadMetadataDraft) => {
      try {
        const created = await addUploads(files, draftToMetadata(draft));
        setUploadStatus({
          text:
            options?.successMessage?.(created.length) ??
            `Uploading ${created.length} clip${created.length === 1 ? "" : "s"} — track progress in Film Room → Upload / Process Film.`,
          failed: false,
        });
        setTimeout(() => setUploadStatus(null), 5000);
      } catch (err) {
        setUploadStatus({
          text: `Upload failed: ${err instanceof Error ? err.message : String(err)}`,
          failed: true,
        });
      }
    },
    [addUploads, options],
  );

  const widget = (
    <>
      <UploadDialog
        open={open}
        onOpenChange={setOpen}
        onSubmit={handleSubmit}
        opponentSuggestions={opponentSuggestions}
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
