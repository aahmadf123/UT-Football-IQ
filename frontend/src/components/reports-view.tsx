"use client";

/**
 * Reports.
 *
 * Backend-wired end to end: create report jobs (POST /api/v1/reports), poll
 * their status, and fetch signed download URLs. The old faux "report preview"
 * mock-up was reduced to an honest summary of the selected sections/format.
 */

import { Download, Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAppState } from "@/lib/app-state";
import {
  createReport,
  getReport,
  getReportDownloadUrl,
  listReports,
} from "@/lib/api";
import { useUploadWidget } from "@/components/shared/upload-widget";
import type { ReportFormat, ReportJob } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { StatusBadge, toneForJobStatus } from "@/components/composite/status-badge";

const REPORT_SECTIONS: readonly string[] = [
  "Self-scout exposure",
  "Position group development",
  "Model quality",
  "Opponent prep package",
] as const;

const REPORT_POLL_INTERVAL_MS = 2000;
const REPORT_POLL_TIMEOUT_MS = 120_000;

export function ReportsView() {
  const { authToken } = useAppState();
  const { openFilePicker, widget } = useUploadWidget();
  const [selections, setSelections] = useState<Record<string, boolean>>({});
  const [format, setFormat] = useState<ReportFormat>("pdf");
  const [reports, setReports] = useState<ReportJob[]>([]);
  const [listStatus, setListStatus] = useState<"idle" | "loading" | "error">("idle");
  const [listError, setListError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const pollHandles = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  const refreshList = useCallback(async () => {
    if (!authToken) {
      setReports([]);
      setListStatus("idle");
      setListError(null);
      return;
    }
    setListStatus("loading");
    setListError(null);
    try {
      const items = await listReports(authToken);
      setReports(items);
      setListStatus("idle");
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err));
      setListStatus("error");
    }
  }, [authToken]);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  // Clean up outstanding polls on unmount.
  useEffect(() => {
    const handles = pollHandles.current;
    return () => {
      handles.forEach((h) => clearTimeout(h));
      handles.clear();
    };
  }, []);

  const pollReport = useCallback(
    (reportId: string) => {
      if (!authToken) return;
      const start = Date.now();
      const tick = async () => {
        try {
          const job = await getReport(reportId, authToken);
          setReports((cur) => cur.map((r) => (r.id === reportId ? job : r)));
          if (job.status === "succeeded" || job.status === "failed" || job.status === "cancelled") {
            return;
          }
          if (Date.now() - start > REPORT_POLL_TIMEOUT_MS) return;
          const h = setTimeout(() => {
            pollHandles.current.delete(h);
            void tick();
          }, REPORT_POLL_INTERVAL_MS);
          pollHandles.current.add(h);
        } catch {
          // On a transient error, stop polling — the user can refresh manually.
        }
      };
      void tick();
    },
    [authToken],
  );

  const handleGenerate = async () => {
    if (!authToken) {
      setGenerateError("You must be signed in to generate a report.");
      return;
    }
    const picked = REPORT_SECTIONS.filter((s) => selections[s] !== false);
    if (picked.length === 0) {
      setGenerateError("Select at least one section to include.");
      return;
    }
    setGenerating(true);
    setGenerateError(null);
    try {
      const job = await createReport(
        { report_type: "coaching_summary", format, sections: [...picked] },
        authToken,
      );
      setReports((cur) => [job, ...cur]);
      pollReport(job.id);
    } catch (err) {
      setGenerateError(err instanceof Error ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  };

  const handleDownload = async (reportId: string) => {
    if (!authToken) return;
    setDownloadingId(reportId);
    try {
      const { download_url } = await getReportDownloadUrl(reportId, authToken);
      window.location.href = download_url;
    } catch (err) {
      setGenerateError(err instanceof Error ? err.message : String(err));
    } finally {
      setDownloadingId(null);
    }
  };

  const pickedSections = REPORT_SECTIONS.filter((s) => selections[s] !== false);

  return (
    <>
      {widget}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Report Builder
            </h2>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {REPORT_SECTIONS.map((label) => {
              const id = `report-section-${label.toLowerCase().replace(/\W+/g, "-")}`;
              return (
                <div key={label} className="flex items-center justify-between gap-3">
                  <Label htmlFor={id} className="text-[0.82rem] font-normal">
                    {label}
                  </Label>
                  <Switch
                    id={id}
                    checked={selections[label] !== false}
                    onCheckedChange={(checked) =>
                      setSelections((cur) => ({ ...cur, [label]: checked }))
                    }
                  />
                </div>
              );
            })}
            <div className="flex flex-col gap-1 border-t border-border-soft pt-3">
              <Label
                htmlFor="report-format"
                className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
              >
                Format
              </Label>
              <NativeSelect
                id="report-format"
                value={format}
                onChange={(e) => setFormat(e.target.value as ReportFormat)}
              >
                <option value="pdf">PDF</option>
                <option value="csv">CSV</option>
                <option value="json">JSON</option>
              </NativeSelect>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button onClick={handleGenerate} disabled={generating || !authToken}>
                <Download className="size-4" /> {generating ? "Requesting…" : "Generate Report"}
              </Button>
              <Button variant="outline" onClick={openFilePicker}>
                <Upload className="size-4" /> Add Film
              </Button>
            </div>
            {!authToken && (
              <p className="text-xs text-muted-foreground">
                Sign in to generate and download reports.
              </p>
            )}
            {generateError && (
              <p role="alert" className="text-xs text-status-danger">
                {generateError}
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Packet Summary
            </h2>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-xs text-muted-foreground">
            <p>The generated report is built from live database aggregates on the backend at request time.</p>
            <p>
              <strong className="text-foreground">Format:</strong> {format.toUpperCase()}
            </p>
            <p>
              <strong className="text-foreground">Sections selected:</strong>{" "}
              {pickedSections.length > 0 ? pickedSections.join(", ") : "none"}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Export Queue
            </h2>
          </CardHeader>
          <CardContent>
            {listStatus === "loading" && (
              <p className="text-xs text-muted-foreground">Loading reports…</p>
            )}
            {listStatus === "error" && (
              <p className="text-xs text-status-danger">
                {listError ?? "Failed to load reports."}
              </p>
            )}
            {listStatus === "idle" && reports.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No reports yet — pick sections and click Generate.
              </p>
            )}
            {reports.length > 0 && (
              <div className="flex flex-col">
                {reports.map((report) => (
                  <ReportRow
                    key={report.id}
                    report={report}
                    downloading={downloadingId === report.id}
                    onDownload={() => handleDownload(report.id)}
                  />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}

function ReportRow({
  report,
  downloading,
  onDownload,
}: {
  report: ReportJob;
  downloading: boolean;
  onDownload: () => void;
}) {
  const stamp = new Date(report.created_at).toLocaleString();
  const subtitle =
    report.status === "failed" && report.error_message
      ? `failed: ${report.error_message}`
      : `${report.format.toUpperCase()} · ${stamp}`;
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border-soft py-2 last:border-b-0">
      <div className="min-w-0">
        <StatusBadge tone={toneForJobStatus(report.status)} dot className="capitalize">
          {report.status}
        </StatusBadge>
        <div className="mt-1 truncate text-xs text-muted-foreground">{subtitle}</div>
      </div>
      {report.status === "succeeded" ? (
        <Button variant="outline" size="sm" onClick={onDownload} disabled={downloading}>
          <Download className="size-3.5" /> {downloading ? "…" : "Download"}
        </Button>
      ) : (
        <span className="shrink-0 text-xs text-muted-foreground">
          {report.status === "failed" ? "—" : "in progress"}
        </span>
      )}
    </div>
  );
}
