"use client";

/**
 * Zero-shot concept search box (Issue #144).
 *
 * Lets a coach type a football concept ("mesh", "cover 3 trips", "jet sweep")
 * and surfaces matching reps from the existing library — no labelling and no
 * Toledo-specific fine-tuning required. The backend grounds the query against
 * structured labels and, when a play-embedding model is promoted, expands it
 * with similar reps from pgvector (Issue #8).
 *
 * Honesty rules (matching the backend contract):
 *   • every result set is marked APPROXIMATE (zero-shot concept→label mapping);
 *   • embedding-expansion rows are additionally marked EXPERIMENTAL;
 *   • in mock mode we never fabricate results — we say search needs a backend.
 */

import { Search } from "lucide-react";
import { useState } from "react";
import { searchConcepts } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import type { ConceptSearchResponse } from "@/lib/types";
import { ExperimentalBadge } from "./experimental-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type SearchState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "mock" }
  | { kind: "done"; response: ConceptSearchResponse };

function shortId(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

function summarizeLabels(labelData: Record<string, unknown> | null): string | null {
  if (!labelData) return null;
  const parts: string[] = [];
  for (const key of ["formation", "coverage"]) {
    const node = labelData[key];
    if (node && typeof node === "object" && "generic" in (node as Record<string, unknown>)) {
      const generic = (node as Record<string, unknown>).generic;
      if (typeof generic === "string") parts.push(generic);
    }
  }
  return parts.length > 0 ? parts.join(" · ") : null;
}

export function ConceptSearch() {
  const { authToken, mockMode } = useAppState();
  const [query, setQuery] = useState("");
  const [state, setState] = useState<SearchState>({ kind: "idle" });

  async function runSearch(e: React.FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    if (mockMode) {
      // Never display mock data as a real search result.
      setState({ kind: "mock" });
      return;
    }
    setState({ kind: "loading" });
    try {
      const response = await searchConcepts(q, { k: 20 }, authToken);
      setState({ kind: "done", response });
    } catch (err) {
      setState({ kind: "error", message: err instanceof Error ? err.message : "Search failed" });
    }
  }

  return (
    <Card data-testid="concept-search">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Concept search
          </h2>
          <ExperimentalBadge label="Approximate" />
        </div>
        <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
          Ask for a concept in plain football — &ldquo;mesh&rdquo;, &ldquo;cover 3 trips&rdquo;,
          &ldquo;jet sweep&rdquo;, &ldquo;play action boot&rdquo;. Results are zero-shot and
          approximate until validated on corrected Toledo clips.
        </p>
      </CardHeader>
      <CardContent>
        <form onSubmit={runSearch} className="flex flex-wrap items-end gap-2">
          <div className="flex min-w-60 flex-1 flex-col gap-1">
            <Label
              htmlFor="concept-query"
              className="font-display text-[0.68rem] font-semibold uppercase tracking-widest text-muted-foreground"
            >
              Concept query
            </Label>
            <Input
              id="concept-query"
              className="h-9"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="e.g. cover 3 trips"
              aria-label="Concept search query"
            />
          </div>
          <Button type="submit" size="sm" className="h-9">
            <Search className="size-4" /> Search
          </Button>
        </form>

        {state.kind === "loading" && (
          <p className="mt-3 text-xs text-muted-foreground">Searching…</p>
        )}

        {state.kind === "mock" && (
          <p className="mt-3 text-xs text-muted-foreground">
            Concept search needs a live backend connection — it is disabled in mock mode so demo
            data is never shown as a real result.
          </p>
        )}

        {state.kind === "error" && (
          <p className="mt-3 text-xs text-status-danger" data-testid="concept-search-error">
            {state.message}
          </p>
        )}

        {state.kind === "done" && <Results response={state.response} />}
      </CardContent>
    </Card>
  );
}

function Results({ response }: { response: ConceptSearchResponse }) {
  return (
    <div className="mt-3" data-testid="concept-search-results">
      {response.matched_concepts.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {response.matched_concepts.map((m) => (
            <Badge key={m.concept_id} variant="secondary">
              {m.display_name}
            </Badge>
          ))}
        </div>
      )}

      {response.experimental && (
        <p className="mb-2 text-xs text-muted-foreground">
          Includes <strong className="text-foreground">experimental</strong> embedding matches —
          reps that look similar but are not yet labelled with this concept.
        </p>
      )}

      {response.results.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {response.reason ?? "No matching reps yet."}
        </p>
      ) : (
        <ul className="m-0 list-none p-0">
          {response.results.map((r) => {
            const labels = summarizeLabels(r.label_data);
            return (
              <li
                key={`${r.clip_id}-${r.source}`}
                className="flex flex-wrap items-center gap-2 border-b border-border-soft py-2 last:border-b-0"
              >
                <code className="font-mono text-[0.78rem]">{shortId(r.clip_id)}</code>
                {labels && <span className="text-xs text-muted-foreground">{labels}</span>}
                <span
                  data-numeric
                  className="ml-auto font-mono text-xs text-muted-foreground"
                  title={`source: ${r.source}`}
                >
                  {r.source} · {(r.confidence * 100).toFixed(0)}%
                </span>
                {r.is_experimental && <ExperimentalBadge />}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
