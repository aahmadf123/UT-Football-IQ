"use client";

/**
 * Two-step film upload: drop the files, then describe the session.
 *
 * The "describe" step is the point. Before it, every upload landed with
 * `session_kind`, `opponent_team` and `recorded_at` all null, which left the
 * Library's opponent and possession filters matching nothing and the Opponent
 * Prep tab permanently empty -- a fully-built feature with no way to ever have
 * data. Metadata is collected once for the batch, because that is how film
 * actually arrives: one practice, one card, thirty clips.
 */

import { useCallback, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import {
  POSSESSIONS,
  SESSION_KINDS,
  SOURCE_TYPES,
  draftFromFiles,
  fromLocalInputValue,
  MAX_TAGS,
  parseTags,
  toLocalInputValue,
  validateDraft,
  type UploadMetadataDraft,
} from "@/lib/upload-metadata";
import type { OurPossession, SessionKind, SourceType } from "@/lib/types";
import type { UploadMetadata } from "@/lib/app-state";

const VIDEO_EXTENSIONS = /\.(mp4|mov|m4v|avi|mkv|mts|m2ts|webm)$/i;

function isVideo(file: File): boolean {
  return file.type.startsWith("video/") || VIDEO_EXTENSIONS.test(file.name);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

export interface UploadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Resolves once the batch has been handed to the upload queue. */
  onSubmit: (files: File[], draft: UploadMetadataDraft) => Promise<void> | void;
  /** Team names offered as opponent suggestions (from the schedule). */
  opponentSuggestions?: string[];
  defaults?: Partial<UploadMetadata>;
  sessionTitle?: string;
}

export function UploadDialog({
  open,
  onOpenChange,
  onSubmit,
  opponentSuggestions = [],
  defaults,
  sessionTitle,
}: UploadDialogProps) {
  const [files, setFiles] = useState<File[]>([]);
  const [draft, setDraft] = useState<UploadMetadataDraft>(() => draftFromFiles([]));
  const [tagInput, setTagInput] = useState("");
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [showErrors, setShowErrors] = useState(false);
  const [rejected, setRejected] = useState<string[]>([]);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const errors = useMemo(() => validateDraft(draft), [draft]);
  const totalBytes = useMemo(() => files.reduce((sum, f) => sum + f.size, 0), [files]);

  const reset = useCallback(() => {
    setFiles([]);
    setDraft(draftFromFiles([]));
    setTagInput("");
    setShowErrors(false);
    setRejected([]);
    setDragging(false);
    setSubmitError(null);
  }, []);

  const addFiles = useCallback(
    (incoming: File[]) => {
      const videos = incoming.filter(isVideo);
      const skipped = incoming.filter((f) => !isVideo(f)).map((f) => f.name);

      setFiles((current) => {
        // De-dupe on name+size: dropping the same folder twice is a common slip
        // and would otherwise upload every clip a second time.
        const seen = new Set(current.map((f) => `${f.name}:${f.size}`));
        const merged = [...current];
        for (const file of videos) {
          const key = `${file.name}:${file.size}`;
          if (!seen.has(key)) {
            seen.add(key);
            merged.push(file);
          }
        }
        // Seed the metadata from the first batch only, so a later drop cannot
        // silently overwrite what the coach has already typed.
        if (current.length === 0 && merged.length > 0) {
          setDraft((existing) => {
            const seeded = draftFromFiles(merged);
            return {
              ...seeded,
              ...existing,
              sessionKind: defaults?.session_kind ?? existing.sessionKind ?? seeded.sessionKind,
              recordedAt: seeded.recordedAt ?? defaults?.recorded_at ?? existing.recordedAt,
              sourceType: existing.sourceType ?? seeded.sourceType,
              opponentTeam: defaults?.opponent_team ?? existing.opponentTeam,
              libraryRole: defaults?.library_role ?? existing.libraryRole ?? "toledo",
            };
          });
        }
        return merged;
      });
      setRejected(skipped);
    },
    [defaults],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(false);
      addFiles(Array.from(event.dataTransfer.files));
    },
    [addFiles],
  );

  const handleSubmit = useCallback(async () => {
    setShowErrors(true);
    if (files.length === 0 || Object.keys(errors).length > 0) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onSubmit(files, draft);
      reset();
      onOpenChange(false);
    } catch (cause) {
      setSubmitError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  }, [draft, errors, files, onOpenChange, onSubmit, reset, tagInput]);

  const update = <K extends keyof UploadMetadataDraft>(key: K, value: UploadMetadataDraft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl" data-testid="upload-dialog">
        <DialogHeader>
          <DialogTitle>Upload film</DialogTitle>
          <DialogDescription>
            Add recordings from any camera or angle. Choose when the film was recorded and whose film it is.
          </DialogDescription>
          {sessionTitle ? <p className="rounded-lg bg-secondary p-3 text-sm">Adding to session: <strong>{sessionTitle}</strong></p> : null}
        </DialogHeader>

        {/* ── Step 1: files ─────────────────────────────────────────────── */}
        <div
          role="button"
          tabIndex={0}
          aria-label="Add film files"
          data-testid="upload-dropzone"
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              inputRef.current?.click();
            }
          }}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed px-4 py-8 text-center transition-colors ${
            dragging ? "border-primary bg-primary/10" : "border-border hover:border-primary/50"
          }`}
        >
          <p className="text-sm font-medium">Drag film here, or click to browse</p>
          <p className="text-muted-foreground mt-1 text-xs">
            Any camera, any angle, any resolution. Whole folders are fine.
          </p>
          <input
            ref={inputRef}
            type="file"
            accept="video/*"
            multiple
            className="hidden"
            data-testid="upload-file-input"
            onChange={(e) => {
              addFiles(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
        </div>

        {rejected.length > 0 && (
          <p className="text-status-warn text-xs" role="status">
            Skipped {rejected.length} non-video file{rejected.length === 1 ? "" : "s"}:{" "}
            {rejected.slice(0, 3).join(", ")}
            {rejected.length > 3 ? "…" : ""}
          </p>
        )}

        {files.length > 0 && (
          <div className="rounded-md border" data-testid="upload-file-list">
            <div className="flex items-center justify-between border-b px-3 py-2 text-xs">
              <span data-numeric>
                {files.length} file{files.length === 1 ? "" : "s"} · {formatBytes(totalBytes)}
              </span>
              <Button variant="ghost" size="sm" onClick={() => setFiles([])} disabled={submitting}>
                Clear
              </Button>
            </div>
            <ul className="max-h-40 overflow-y-auto">
              {files.map((file) => (
                <li
                  key={`${file.name}:${file.size}`}
                  className="flex items-center justify-between gap-2 px-3 py-1.5 text-xs"
                >
                  <span className="truncate">{file.name}</span>
                  <span className="text-muted-foreground shrink-0" data-numeric>
                    {formatBytes(file.size)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ── Step 2: describe ──────────────────────────────────────────── */}
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <Label htmlFor="upload-library-role">Film belongs to</Label>
            <NativeSelect id="upload-library-role" value={draft.libraryRole ?? "toledo"} disabled={Boolean(defaults?.library_role)} onChange={(event) => update("libraryRole", event.target.value as "toledo" | "opponent")}>
              <option value="toledo">Toledo</option><option value="opponent">Opponent scouting</option>
            </NativeSelect>
          </div>
          <div className="grid gap-1.5"><Label htmlFor="upload-camera-label">Camera angle name, optional</Label><Input id="upload-camera-label" value={draft.cameraLabel ?? ""} onChange={(event) => update("cameraLabel", event.target.value || null)} placeholder="Sideline, end zone, drone…" maxLength={120} /></div>
          <div className="grid gap-1.5">
            <Label htmlFor="upload-session-kind">Session type</Label>
            <NativeSelect
              id="upload-session-kind"
              disabled={Boolean(defaults?.practice_session_id)}
              value={draft.sessionKind ?? ""}
              onChange={(e) => update("sessionKind", (e.target.value || null) as SessionKind | null)}
              aria-invalid={showErrors && !!errors.sessionKind}
            >
              <option value="">Select…</option>
              {SESSION_KINDS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </NativeSelect>
            {showErrors && errors.sessionKind && (
              <p className="text-status-danger text-xs">{errors.sessionKind}</p>
            )}
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="upload-recorded-at">Date recorded</Label>
            <Input
              id="upload-recorded-at"
              type="datetime-local"
              value={toLocalInputValue(draft.recordedAt)}
              onChange={(e) => update("recordedAt", fromLocalInputValue(e.target.value))}
              aria-invalid={showErrors && !!errors.recordedAt}
            />
            {showErrors && errors.recordedAt ? (
              <p className="text-status-danger text-xs">{errors.recordedAt}</p>
            ) : (
              draft.recordedAt && (
                <p className="text-muted-foreground text-xs">Read from the filename — edit if wrong.</p>
              )
            )}
          </div>

          {draft.sessionKind === "game" && (
            <div className="grid gap-1.5">
              <Label htmlFor="upload-opponent">Opponent</Label>
              <Input
                id="upload-opponent"
                disabled={Boolean(defaults?.practice_session_id)}
                list="upload-opponent-options"
                placeholder="e.g. Ball State"
                value={draft.opponentTeam ?? ""}
                onChange={(e) => update("opponentTeam", e.target.value || null)}
                aria-invalid={showErrors && !!errors.opponentTeam}
              />
              <datalist id="upload-opponent-options">
                {opponentSuggestions.map((team) => (
                  <option key={team} value={team} />
                ))}
              </datalist>
              {showErrors && errors.opponentTeam && (
                <p className="text-status-danger text-xs">{errors.opponentTeam}</p>
              )}
            </div>
          )}

          <div className="grid gap-1.5">
            <Label htmlFor="upload-possession">Our possession</Label>
            <NativeSelect
              id="upload-possession"
              value={draft.ourPossession ?? ""}
              onChange={(e) =>
                update("ourPossession", (e.target.value || null) as OurPossession | null)
              }
            >
              <option value="">Not specified</option>
              {POSSESSIONS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </NativeSelect>
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="upload-source">Camera</Label>
            <NativeSelect
              id="upload-source"
              value={draft.sourceType ?? ""}
              onChange={(e) => update("sourceType", (e.target.value || null) as SourceType | null)}
            >
              <option value="">Not specified</option>
              {SOURCE_TYPES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </NativeSelect>
          </div>

          <div className="grid gap-1.5 sm:col-span-2">
            <Label htmlFor="upload-tags">Tags</Label>
            <Input
              id="upload-tags"
              placeholder="red zone, third down, install"
              value={tagInput}
              onChange={(e) => {
                // Parsed into the draft as they are typed, not at submit, so
                // validateDraft can see the count. Left until submit, going
                // over the cap uploads the whole batch and only then 422s,
                // orphaning every object in it.
                setTagInput(e.target.value);
                update("tags", parseTags(e.target.value));
              }}
              aria-invalid={showErrors && !!errors.tags}
            />
            {showErrors && errors.tags ? (
              <p className="text-status-danger text-xs">{errors.tags}</p>
            ) : (
              <p className="text-muted-foreground text-xs">
                Comma or line separated. Optional, up to {MAX_TAGS}.
              </p>
            )}
          </div>
        </div>
        {submitError ? <p role="alert" className="rounded-lg border border-status-danger/40 p-3 text-sm text-status-danger">Upload failed: {submitError}</p> : null}

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={submitting || files.length === 0}
            data-testid="upload-submit"
          >
            {submitting
              ? "Starting…"
              : `Upload ${files.length || ""} file${files.length === 1 ? "" : "s"}`.trim()}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
