"use client";

import { useState } from "react";
import { Download, Upload } from "lucide-react";
import { useAppState } from "@/lib/app-state";
import { exportSessionArchive, importSessionArchive, type SessionImportPreview } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { friendlyMessage } from "@/lib/api-error";

export function SessionTransfer({ sessionId, onImported }: { sessionId?: string; onImported: (id: string) => Promise<void> | void }) {
  const { authToken, refreshData, currentRole } = useAppState();
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SessionImportPreview | null>(null);
  const [busy, setBusy] = useState<"export" | "preview" | "import" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function exportArchive() {
    if (!sessionId) return;
    setBusy("export"); setError(null); setNotice(null);
    try {
      const result = await exportSessionArchive(sessionId, authToken);
      const link = document.createElement("a"); link.href = result.download_url; link.download = `session-${sessionId}.zip`; link.target = "_blank"; link.rel = "noopener noreferrer"; document.body.appendChild(link); link.click(); link.remove();
      setNotice(`Session archive ready (${(result.size_bytes / 1024 ** 2).toFixed(1)} MB). Your browser handles the download.`);
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(null); }
  }

  async function importArchive(apply: boolean) {
    if (!file) return;
    setBusy(apply ? "import" : "preview"); setError(null);
    try {
      const result = await importSessionArchive(file, !apply, authToken);
      setPreview(result);
      if (apply) { await refreshData(); await onImported(result.session_id); setOpen(false); setFile(null); setPreview(null); setNotice(`Imported ${result.title} into this local library.`); }
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(null); }
  }

  if (currentRole !== "admin" && currentRole !== "analyst") return null;

  return <>
    <div className="flex flex-wrap gap-2">{sessionId ? <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void exportArchive()}><Download className="size-4" />{busy === "export" ? "Preparing archive…" : "Export session"}</Button> : null}<Button size="sm" variant="outline" disabled={busy !== null} onClick={() => { setOpen(true); setError(null); setPreview(null); setFile(null); }}><Upload className="size-4" /> Import session</Button></div>
    {notice ? <p role="status" className="basis-full text-xs text-muted-foreground">{notice}</p> : null}
    {error && !open ? <p role="alert" className="basis-full text-xs text-status-danger">{error}</p> : null}
    <Dialog open={open} onOpenChange={(value) => { if (!busy) setOpen(value); }}><DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl"><DialogHeader><DialogTitle>Import a session archive</DialogTitle><DialogDescription>Bring film and saved annotations from another local installation into this library. Preview the archive before applying it.</DialogDescription></DialogHeader><div className="flex flex-col gap-4">
      <Label htmlFor="session-archive-file">Session ZIP archive</Label><Input id="session-archive-file" type="file" accept=".zip,application/zip" disabled={busy !== null} onChange={(event) => { setFile(event.target.files?.[0] ?? null); setPreview(null); setError(null); }} />
      {file ? <p className="text-sm text-muted-foreground">{file.name} · {(file.size / 1024 ** 2).toFixed(1)} MB</p> : null}
      {preview ? <div className="rounded-lg border border-border-soft p-4"><h3 className="font-medium">{preview.title}</h3><p className="mt-2 text-sm text-muted-foreground">{preview.created} records to import · {preview.skipped} already present · {preview.media_count} media files</p>{preview.conflicts.length ? <div role="alert" className="mt-3 text-sm text-status-danger"><p>This archive conflicts with existing library records.</p><ul className="mt-2 list-disc space-y-1 pl-5">{preview.conflicts.map((conflict) => <li key={`${conflict.table}-${conflict.id}`}>{conflict.reason}</li>)}</ul></div> : <p className="mt-3 text-sm text-status-ok">No conflicts found.</p>}</div> : null}
      {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
      <DialogFooter><Button variant="outline" disabled={busy !== null} onClick={() => setOpen(false)}>Cancel</Button><Button variant="outline" disabled={!file || busy !== null} onClick={() => void importArchive(false)}>{busy === "preview" ? "Previewing…" : "Preview archive"}</Button>{preview ? <Button disabled={busy !== null || preview.conflicts.length > 0} onClick={() => void importArchive(true)}>{busy === "import" ? "Importing…" : "Import session"}</Button> : null}</DialogFooter>
    </div></DialogContent></Dialog>
  </>;
}
