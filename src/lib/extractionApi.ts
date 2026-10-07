import type { ParseResult } from "./documentParser";

const backendFormats =
  /\.(pdf|docx|xlsx|xls|csv|tsv|txt|json|png|jpe?g|webp|bmp|tiff?)$/i;

export const extractionFileAccept =
  ".pdf,.docx,.xlsx,.xls,.csv,.tsv,.txt,.json,.png,.jpg,.jpeg,.webp,.bmp,.tiff,.tif";

export const canUseExtractionApi = (file: File) =>
  backendFormats.test(file.name);

export type ExtractionModel = {
  id: string;
  name: string;
  description: string;
  size: string;
  recommended?: boolean;
  configured?: boolean;
  installed?: boolean;
};

const notifyUnauthorized = (status: number) => {
  if (status === 401)
    window.dispatchEvent(new Event("readdocument-auth-required"));
};

export const getExtractionModels = async (): Promise<ExtractionModel[]> => {
  const response = await fetch("/api/models", {
    credentials: "include",
    signal: AbortSignal.timeout(4000),
  });
  if (!response.ok) {
    notifyUnauthorized(response.status);
    throw new Error("Не удалось получить список моделей");
  }
  const payload = (await response.json()) as { models: ExtractionModel[] };
  return payload.models;
};

export const parseWithExtractionApi = async (
  file: File,
  models: string[],
): Promise<ParseResult> => {
  const body = new FormData();
  body.append("file", file);
  body.append("models", JSON.stringify(models));
  let response: Response;
  try {
    response = await fetch("/api/extract", {
      method: "POST",
      body,
      credentials: "include",
      signal: AbortSignal.timeout(750_000),
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "TimeoutError")
      throw new Error("Время ожидания OpenAI API истекло. Повторите чтение документа.", { cause });
    throw new Error("Не удалось связаться с сервером распознавания. Проверьте подключение и повторите чтение.", { cause });
  }
  if (!response.ok) {
    notifyUnauthorized(response.status);
    const payload = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new Error(
      typeof payload.detail === "string" ? payload.detail :
      `Сервер распознавания вернул ${response.status}`,
    );
  }
  return response.json() as Promise<ParseResult>;
};

export type SavedProposal = {
  id: string;
  createdAt: string;
  status: "saved";
  duplicate: boolean;
};

export const submitProposal = async (
  payload: { proposal: unknown; sources: unknown[] },
  key: string,
): Promise<SavedProposal> => {
  const response = await fetch("/api/proposals", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    notifyUnauthorized(response.status);
    const result = (await response.json().catch(() => ({}))) as {
      detail?: string;
    };
    throw new Error(
      result.detail || `Сервер сохранения вернул ${response.status}`,
    );
  }
  return response.json() as Promise<SavedProposal>;
};
