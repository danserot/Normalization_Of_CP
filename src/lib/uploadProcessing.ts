import {
  canUseExtractionApi,
  parseWithExtractionApi,
} from "./extractionApi";
import type {
  CommercialProposal,
  ExtractionMetadata,
} from "../types/extraction";

export type DocumentUpload = {
  file: File;
  proposal: Partial<CommercialProposal>;
  metadata: ExtractionMetadata;
  error?: string;
};

export type ReadProgress = { done: number; total: number; current: string };

export const isSuccessfulUpload = (upload: DocumentUpload) => !upload.error;

export const readableExtractionMessage = (message: string): string => {
  if (/ConnectError|не удалось связаться|UNEXPECTED_EOF/i.test(message))
    return "Нет соединения с сервисом распознавания. Повторите чтение после восстановления подключения.";
  return message
    .replace(/\((?:ConnectTimeout|ReadTimeout|TimeoutException)\)/g, "")
    .trim();
};

export function unavailableLabel(reason?: "absent" | "unreadable" | "unverified") {
  return reason === "absent" ? "Не указано в КП"
    : reason === "unreadable" ? "Не удалось прочитать"
    : "Требуется проверка";
}

function failedUpload(file: File, message: string): DocumentUpload {
  const error = readableExtractionMessage(message);
  return {
    file,
    proposal: {},
    metadata: {
      sourceName: file.name,
      parser: "Распознавание документа",
      status: "error",
      confidence: 0,
      warnings: [error],
    },
    error,
  };
}

async function readDocument(file: File, index: number): Promise<DocumentUpload> {
  if (index >= 50)
    return failedUpload(file, "За одно чтение можно загрузить не больше 50 файлов");
  if (file.size > 25 * 1024 * 1024)
    return failedUpload(file, "Файл превышает 25 МБ");
  if (!canUseExtractionApi(file))
    return failedUpload(file, "Формат файла не поддерживается");
  try {
    const { proposal, metadata } = await parseWithExtractionApi(file, ["openai"]);
    const failed = metadata.status === "error" || metadata.status === "unsupported"
      || (metadata.status === "empty" && (!metadata.outcome
        || metadata.outcome.unavailable.some((entry) => entry.reason !== "absent")));
    return {
      file,
      proposal,
      metadata,
      error: failed
        ? readableExtractionMessage(metadata.warnings[0] || metadata.outcome?.message || "Не удалось прочитать документ")
        : undefined,
    };
  } catch (cause) {
    return failedUpload(file, cause instanceof Error ? cause.message : "Не удалось извлечь данные");
  }
}

/** Keep completed files visible and preserve input order while two requests run. */
export async function readDocuments(
  files: File[],
  onProgress: (progress: ReadProgress) => void,
  onResults: (results: DocumentUpload[]) => void,
): Promise<DocumentUpload[]> {
  const results: Array<DocumentUpload | undefined> = new Array(files.length);
  const activeFiles = new Map<number, string>();
  let nextIndex = 0;
  let done = 0;
  const completed = () => results.filter((upload): upload is DocumentUpload => !!upload);
  const reportProgress = () => onProgress({
    done,
    total: files.length,
    current: [...activeFiles.values()].join(" · "),
  });
  const worker = async () => {
    while (nextIndex < files.length) {
      const index = nextIndex++;
      activeFiles.set(index, files[index].name);
      reportProgress();
      results[index] = await readDocument(files[index], index);
      activeFiles.delete(index);
      done += 1;
      onResults(completed());
      reportProgress();
    }
  };
  await Promise.all(Array.from({ length: Math.min(2, files.length) }, worker));
  return completed();
}
