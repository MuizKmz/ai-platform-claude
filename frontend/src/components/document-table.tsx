"use client";

import { useState } from "react";
import { Archive, Loader2, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { DocumentSummary } from "@/lib/api";

/**
 * The document list.
 *
 * SOLID, not glass. Everything else in this console is a Liquid Glass surface,
 * and this deliberately is not: a table is dense text that people scan, and
 * reading a chunk count or a source path through a refracting lens is the one
 * place the material fights the tool. Apple's own guidance says the same about
 * putting Liquid Glass behind reading content.
 *
 * So the glass stays on the chrome — panels, dialogs, the composer — and the
 * data sits on a plain surface where it can be read.
 */

interface DocumentTableProps {
  documents: DocumentSummary[];
  canDelete: boolean;
  onDelete: (id: string) => Promise<void>;
}

export function DocumentTable({
  documents,
  canDelete,
  onDelete,
}: DocumentTableProps) {
  const [deleting, setDeleting] = useState<string | null>(null);

  async function remove(id: string) {
    setDeleting(id);
    try {
      await onDelete(id);
    } finally {
      setDeleting(null);
    }
  }

  if (documents.length === 0) {
    return (
      <p className="text-muted-foreground py-8 text-sm">
        No documents you can see. Ingest some with the CLI, or start a job
        below.
      </p>
    );
  }

  return (
    <div className="border-border/60 bg-card/40 overflow-hidden rounded-lg border">
      <Table>
        <TableHeader>
          <TableRow className="border-border/60 hover:bg-transparent">
            <TableHead className="text-xs">Title</TableHead>
            <TableHead className="text-xs">Labels</TableHead>
            <TableHead className="text-right text-xs">Chunks</TableHead>
            <TableHead className="text-right text-xs">Version</TableHead>
            <TableHead className="w-10" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {documents.map((doc) => (
            <TableRow
              key={doc.id}
              className="border-border/40"
              // Superseded rows are dimmed rather than hidden: they are
              // retained so an already-issued citation still resolves, and
              // pretending they do not exist would make that confusing.
              data-superseded={doc.superseded_at ? "" : undefined}
            >
              <TableCell className="max-w-0">
                <div className="flex items-center gap-2">
                  {doc.superseded_at ? (
                    <Archive
                      className="text-muted-foreground size-3.5 shrink-0"
                      aria-label="Superseded"
                    />
                  ) : null}
                  <div className="min-w-0">
                    <p
                      className={`truncate text-sm ${doc.superseded_at ? "text-muted-foreground" : ""}`}
                    >
                      {doc.title}
                    </p>
                    {/* The source path is what someone needs to re-ingest or
                        find the original, so it is worth the second line. */}
                    <p className="text-muted-foreground/70 truncate font-mono text-[11px]">
                      {doc.source_path}
                    </p>
                  </div>
                </div>
              </TableCell>

              <TableCell>
                <div className="flex flex-wrap gap-1">
                  {doc.labels.length === 0 ? (
                    // An unlabelled document is visible to nobody. Saying so
                    // is more useful than an empty cell, because it looks like
                    // a mistake and usually is one.
                    <Badge
                      variant="outline"
                      className="text-muted-foreground h-5 px-1.5 text-[10px] font-normal"
                      title="A document with no labels is visible to nobody"
                    >
                      none
                    </Badge>
                  ) : (
                    doc.labels.map((label) => (
                      <Badge
                        key={label}
                        variant="secondary"
                        className="h-5 px-1.5 font-mono text-[10px] font-normal"
                      >
                        {label}
                      </Badge>
                    ))
                  )}
                </div>
              </TableCell>

              <TableCell className="text-right font-mono text-xs">
                {doc.chunk_count}
              </TableCell>

              <TableCell className="text-muted-foreground text-right font-mono text-xs">
                v{doc.version}
              </TableCell>

              <TableCell>
                {canDelete && !doc.superseded_at ? (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="text-muted-foreground hover:text-destructive size-7"
                    onClick={() => void remove(doc.id)}
                    disabled={deleting === doc.id}
                    aria-label={`Delete ${doc.title}`}
                  >
                    {deleting === doc.id ? (
                      <Loader2 className="size-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="size-3.5" />
                    )}
                  </Button>
                ) : null}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
