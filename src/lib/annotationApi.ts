import type { DocumentRouting, SourceCell } from "./documentParser";
export type { SourceCell } from "./documentParser";
export type MarkedField = { field: string; state: "pending" | "found" | "missing"; cell: string; value: string };
export type MarkedTable = {
  block: string; reviewed: boolean; isItems: boolean; firstRow: number; lastRow: number;
  nameColumn: number; quantityColumn: number; unitColumn: number;
  componentMode?: "additive" | "alternative";
  unitPriceColumn?: number; lineTotalColumn?: number;
  components: { label: string; labelCell: string; priceColumn: number; totalColumn: number }[];
  extras: { label: string; labelCell: string; column: number }[];
};
export type Annotation = { fields: MarkedField[]; tables: MarkedTable[]; issues: string[]; group: string; notes: string };
export type AnnotationSummary = {
  id: string; filename: string; status: "draft" | "reviewed"; revision: number; createdAt: string; updatedAt: string;
};
export type AnnotationDocument = AnnotationSummary & {
  cells: SourceCell[]; annotation: Annotation; warnings: string[]; parseError: string;
  routing?: DocumentRouting;
  preview: { kind: "pdf" | "image" | "word" | "text" | "table"; pages?: number; text?: string };
  duplicate?: boolean;
  automation?: { status?: string; humanReviewed?: boolean; split?: string; reasons?: string[]; quarantined?: boolean; quarantineReasons?: string[] };
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/annotations${path}`, { credentials: "include", ...init });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("readdocument-auth-required"));
    const result = await response.json().catch(() => ({}));
    throw new Error(typeof result.detail === "string" ? result.detail : `Ошибка сервера: ${response.status}`);
  }
  return response.json() as Promise<T>;
}
export const listAnnotations = () => request<{ documents: AnnotationSummary[] }>("");
export const getAnnotation = (id: string) => request<AnnotationDocument>(`/${id}`);
export const uploadAnnotation = (file: File) => {
  const body = new FormData(); body.append("file", file);
  return request<AnnotationDocument>("", { method: "POST", body, signal: AbortSignal.timeout(620_000) });
};
export const saveAnnotation = (doc: AnnotationDocument, status: "draft" | "reviewed") =>
  request<AnnotationDocument>(`/${doc.id}`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision: doc.revision, status, annotation: doc.annotation }) });
export async function exportAnnotations() {
  const response = await fetch("/api/annotations/export", { credentials: "include" });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("readdocument-auth-required"));
    const result = await response.json().catch(() => ({}));
    throw new Error(result.detail || "Не удалось выгрузить датасет");
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = "kp-dataset.zip";
  anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
