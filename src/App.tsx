import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, FormEvent } from "react";
import {
  canUseExtractionApi,
  getExtractionModels,
  parseWithExtractionApi,
  submitProposal,
} from "./lib/extractionApi";
import type { ExtractionModel } from "./lib/extractionApi";
import type {
  CommercialProposal,
  ExtractionMetadata,
  FieldEvidence,
  ModelRun,
} from "./lib/documentParser";
import "./App.css";
import AnnotationWorkspace from "./AnnotationWorkspace";

type Upload = {
  file: File;
  proposal: Partial<CommercialProposal>;
  metadata: ExtractionMetadata;
  error?: string;
};
function EvidenceExplanation({ metadata }: { metadata: ExtractionMetadata }) {
  const entries = Object.values(metadata.fieldEvidence ?? {});
  const scored = entries.filter(
    (entry): entry is FieldEvidence =>
      !Array.isArray(entry) && typeof entry.confidence === "number",
  );
  const conflicts = entries.filter(Array.isArray).length;
  return (
    <details className="confidence-explanation">
      <summary>Как читать подтверждения источника?</summary>
      <p>
        Ссылка показывает, из какой ячейки или фрагмента взято значение. Это
        помогает найти его в документе, но само совпадение текста не гарантирует,
        что поле выбрано по смыслу правильно.
      </p>
      <ul>
        <li>
          Значений с привязанным источником: {scored.length}.
        </li>
        <li>
          Конфликтующих значений: {conflicts}.
        </li>
      </ul>
      <p>
        Перед сохранением проверьте смысл поля, OCR, конфликты и суммы по
        оригиналу.
      </p>
    </details>
  );
}
const builtInModels: ExtractionModel[] = [
  {
    id: "local",
    name: "Локальное извлечение",
    description: "Универсальное извлечение структуры КП и проверка источников",
    size: "CPU по умолчанию; GPU-профиль при настройке",
    recommended: true,
  },
];
const defaultComparisonModels = builtInModels.map(({ id }) => id);
const blank: CommercialProposal = {
  title: "",
  client: "",
  clientContact: "",
  validUntil: "",
  supplier: "",
  currency: "",
  vat: "",
  discount: "",
  delivery: "",
  paymentTerms: "",
  deliveryTerms: "",
  warranty: "",
  documentNumber: "",
  documentDate: "",
  documentTotal: null,
  notes: "",
  items: [],
};
const textFields: Array<[keyof CommercialProposal, string]> = [
  ["title", "Название"],
  ["client", "Клиент"],
  ["clientContact", "Контакт"],
  ["validUntil", "Срок действия"],
  ["supplier", "Поставщик"],
  ["documentNumber", "Номер документа"],
  ["documentDate", "Дата документа"],
  ["currency", "Валюта"],
  ["vat", "НДС"],
  ["discount", "Скидка"],
  ["delivery", "Доставка"],
  ["paymentTerms", "Условия оплаты"],
  ["deliveryTerms", "Сроки поставки"],
  ["warranty", "Гарантия"],
];
const money = (value: number | null, currency: string) => {
  if (value === null) return "Не удалось извлечь";

  const known =
    (
      {
        "руб.": "RUB",
        руб: "RUB",
        "₽": "RUB",
        тенге: "KZT",
        "₸": "KZT",
      } as Record<string, string>
    )[currency] ?? currency.toUpperCase();
  if (!currency.trim())
    return `${value.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} · валюта не указана`;
  try {
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency: known,
      maximumFractionDigits: 2,
    }).format(value);
  } catch {
    return `${value.toLocaleString("ru-RU")} ${currency}`;
  }
};
const itemSum = (item: CommercialProposal["items"][number]): number | null =>
  item.lineTotal ??
  (item.quantity === null || item.unitPrice === null ?
    null
  : item.quantity * item.unitPrice);
const sumItems = (items: CommercialProposal["items"]): number | null =>
  items.some((item) => itemSum(item) === null) ? null : (
    items.reduce((sum, item) => sum + (itemSum(item) ?? 0), 0)
  );
const asEvidence = (
  value: FieldEvidence | FieldEvidence[] | undefined,
): FieldEvidence[] =>
  !value ? []
  : Array.isArray(value) ? value
  : [value];

const mergeUploads = (uploads: Upload[]): CommercialProposal =>
  uploads
    .filter((upload) => !upload.error)
    .reduce<CommercialProposal>(
      (current, { proposal: next }) => {
        const update = { ...current };
        for (const [key] of textFields) {
          const value = next[key];
          if (
            typeof value === "string" &&
            value.trim() &&
            (!update[key] || (key === "title" && update[key] === blank.title))
          )
            update[key] = value as never;
        }
        if (
          typeof next.documentTotal === "number" &&
          next.documentTotal >= 0 &&
          update.documentTotal === null
        )
          update.documentTotal = next.documentTotal;
        update.notes = [current.notes, next.notes]
          .filter(
            (part): part is string => typeof part === "string" && !!part.trim(),
          )
          .join("\n\n");
        update.items = [...current.items, ...(next.items ?? [])];
        update.additionalFields = [
          ...(current.additionalFields ?? []),
          ...(next.additionalFields ?? []),
        ];
        return update;
      },
      { ...blank, items: [] },
    );

function App() {
  const [workspace, setWorkspace] = useState<"reader" | "annotation">("reader");
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [proposal, setProposal] = useState<CommercialProposal>(blank);
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);
  const [readProgress, setReadProgress] = useState({ done: 0, total: 0, current: "" });
  const [dragging, setDragging] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [saved, setSaved] = useState<{ id: string; createdAt: string } | null>(
    null,
  );
  const [previewFile, setPreviewFile] = useState<string | null>(null);
  const [resolvedConflicts, setResolvedConflicts] = useState<string[]>([]);
  const [sessionChecked, setSessionChecked] = useState(false);
  const [authorized, setAuthorized] = useState(true);
  const [passwordRequired, setPasswordRequired] = useState(false);
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [models, setModels] = useState<ExtractionModel[]>(builtInModels);
  const [selectionMode, setSelectionMode] = useState<"single" | "compare">(
    "single",
  );
  const [selectedModels, setSelectedModels] = useState<string[]>(["local"]);
  const inputRef = useRef<HTMLInputElement>(null);
  const editOriginal = useRef<CommercialProposal>(blank);
  const submissionKey = useRef(crypto.randomUUID());
  useEffect(() => {
    let active = true;
    const onUnauthorized = () => {
      setPasswordRequired(true);
      setAuthorized(false);
    };
    window.addEventListener("readdocument-auth-required", onUnauthorized);
    void fetch("/api/session", {
      credentials: "include",
      signal: AbortSignal.timeout(3000),
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Не удалось проверить сессию");
        return response.json() as Promise<{
          authenticated: boolean;
          passwordRequired: boolean;
        }>;
      })
      .then((session) => {
        if (active) {
          setAuthorized(session.authenticated);
          setPasswordRequired(session.passwordRequired);
        }
      })
      .catch(() => {
        if (active) {
          setAuthorized(true);
          setPasswordRequired(false);
        }
      })
      .finally(() => {
        if (active) setSessionChecked(true);
      });
    return () => {
      active = false;
      window.removeEventListener("readdocument-auth-required", onUnauthorized);
    };
  }, []);
  useEffect(() => {
    if (!sessionChecked || !authorized) return;
    let active = true;
    void getExtractionModels()
      .then((availableModels) => {
        if (!active || !availableModels.length) return;
        setModels(availableModels);
        setSelectedModels((current) => {
          const valid = current.filter((id) =>
            availableModels.some((model) => model.id === id),
          );
          return valid.length ? valid : [availableModels[0].id];
        });
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [authorized, sessionChecked]);
  const total = useMemo(() => sumItems(proposal.items), [proposal.items]);

  const changeSelectionMode = (mode: "single" | "compare") => {
    setSelectionMode(mode);
    setSelectedModels((current) =>
      mode === "single" ?
        [current[0] ?? models[0].id]
      : Array.from(
          new Set([
            current[0] ?? models[0].id,
            ...models
              .filter(({ id }) => defaultComparisonModels.includes(id))
              .map(({ id }) => id),
            ...models.map(({ id }) => id),
          ]),
        ).slice(0, 4),
    );
  };
  const toggleModel = (modelId: string) => {
    setSelectedModels((current) => {
      if (selectionMode === "single") return [modelId];
      if (current.includes(modelId))
        return current.length > 2 ?
            current.filter((id) => id !== modelId)
          : current;
      return current.length < 4 ? [...current, modelId] : current;
    });
  };

  const processFiles = async (files: File[]) => {
    const acceptable = files.map((file, index) => ({
      file,
      error:
        index >= 50 ? "За одно чтение можно загрузить не больше 50 файлов"
        : file.size > 25 * 1024 * 1024 ? "Файл превышает 25 МБ"
        : "",
    }));
    if (!acceptable.length) return;
    setError("");
    if (reading) return;
    setReading(true);
    setUploads([]);
    setReadProgress({ done: 0, total: acceptable.length, current: acceptable[0]?.file.name ?? "" });
    setProposal({ ...blank, items: [] });
    setConfirmed(false);
    setSaved(null);
    submissionKey.current = crypto.randomUUID();
    try {
      const results: Upload[] = [];
      for (const { file, error: sizeError } of acceptable) {
        const upload = await (async (): Promise<Upload> => {
          if (sizeError)
            return {
              file,
              proposal: {},
              metadata: {
                sourceName: file.name,
                parser: "Проверка файла",
                status: "error",
                confidence: 0,
                warnings: [sizeError],
              },
              error: sizeError,
            };
          try {
            if (!canUseExtractionApi(file))
              throw new Error("Формат файла не поддерживается");
            const result = await parseWithExtractionApi(file, selectedModels);
            return {
              file,
              proposal: result.proposal,
              metadata: result.metadata,
              error:
                (
                  result.metadata.status === "error" ||
                  result.metadata.status === "unsupported" ||
                  result.metadata.status === "empty"
                ) ?
                  result.metadata.warnings[0]
                : undefined,
            };
          } catch (cause) {
            return {
              file,
              proposal: {},
              metadata: {
                sourceName: file.name,
                parser: "Локальное извлечение",
                status: "error",
                confidence: 0,
                warnings: [
                  cause instanceof Error ?
                    cause.message
                  : "Не удалось извлечь данные",
                ],
              },
              error:
                cause instanceof Error ?
                  cause.message
                : "Не удалось извлечь данные",
            };
          }
        })();
        results.push(upload);
        setUploads([...results]);
        setReadProgress({
          done: results.length,
          total: acceptable.length,
          current: acceptable[results.length]?.file.name ?? "",
        });
      }
      const successful = results.filter((upload) => !upload.error);
      setProposal(mergeUploads(successful));
      setEditing(false);
      setConfirmed(false);
      setResolvedConflicts([]);
      setPreviewFile(null);
      if (successful.length === 0)
        setError(
          "Не удалось извлечь данные. Подробности указаны рядом с файлами.",
        );
    } finally {
      setReading(false);
    }
  };

  const onInput = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) void processFiles(Array.from(event.target.files));
    event.target.value = "";
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void processFiles(Array.from(event.dataTransfer.files));
  };
  const reset = () => {
    setUploads([]);
    setProposal(blank);
    setError("");
    setEditing(false);
    setConfirmed(false);
    setSaved(null);
    submissionKey.current = crypto.randomUUID();
    if (inputRef.current) inputRef.current.value = "";
  };
  const login = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthError("");
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const result = (await response.json()) as {
        authenticated?: boolean;
        detail?: string;
      };
      if (!response.ok || !result.authenticated)
        throw new Error(result.detail || "Не удалось войти");
      setAuthorized(true);
      setPasswordRequired(true);
      setPassword("");
    } catch (cause) {
      setAuthError(cause instanceof Error ? cause.message : "Не удалось войти");
    }
  };
  const logout = async () => {
    await fetch("/api/logout", {
      method: "POST",
      credentials: "include",
    }).catch(() => undefined);
    reset();
    setAuthorized(false);
  };

  const conflicts = [
    ...textFields,
    ["documentTotal", "Итог документа"] as [keyof CommercialProposal, string],
  ].flatMap(([key, label]) => {
    const values = uploads.flatMap((upload) => {
      const candidates = [
        ...asEvidence(upload.metadata.fieldEvidence?.[key])
          .filter((e) => e.value !== undefined)
          .map((e) => ({
            proposal: { [key]: e.value },
            source: upload.file.name,
          })),
        { proposal: upload.proposal, source: `${upload.file.name} · итог` },
        ...(upload.metadata.modelRuns ?? [])
          .filter((run) => run.status === "parsed" && run.proposal)
          .map((run) => ({
            proposal: run.proposal!,
            source: `${upload.file.name} · ${run.name}`,
          })),
      ];
      return candidates.flatMap(({ proposal: candidate, source }) => {
        const raw = candidate[key as keyof typeof candidate];
        const value = typeof raw === "number" ? String(raw) : raw;
        return !upload.error && typeof value === "string" && value.trim() ?
            [{ value, file: source }]
          : [];
      });
    });
    const uniqueValues = values.filter(
      ({ value }, index) =>
        values.findIndex(
          (candidate) =>
            candidate.value.trim().toLocaleLowerCase() ===
            value.trim().toLocaleLowerCase(),
        ) === index,
    );
    return uniqueValues.length > 1 ?
        [{ key, label, values: uniqueValues }]
      : [];
  });
  const itemEvidence = (item: CommercialProposal["items"][number]) =>
    uploads
      .filter((upload) => !upload.error)
      .flatMap((upload) =>
        (upload.proposal.items ?? []).flatMap((original, index) =>
          (
            original.name === item.name &&
            original.quantity === item.quantity &&
            original.unitPrice === item.unitPrice &&
            original.unit === item.unit
          ) ?
            Object.entries(upload.metadata.fieldEvidence ?? {})
              .filter(([key]) => key.startsWith(`items.${index}.`))
              .flatMap(([, value]) => asEvidence(value))
          : [],
        ),
      );
  const duplicateItems = proposal.items.filter(
    (item, index) =>
      proposal.items.findIndex(
        (candidate) =>
          candidate.name.trim().toLowerCase() ===
          item.name.trim().toLowerCase(),
      ) !== index,
  );
  const validation = [
    ...(!proposal.client.trim() ? ["Укажите клиента"] : []),
    ...(!proposal.validUntil.trim() ? ["Уточните срок действия"] : []),
    ...(!proposal.items.length ? ["Добавьте хотя бы одну позицию"] : []),
    ...proposal.items.flatMap((item, index) => [
      ...(!item.name.trim() ?
        [`Позиция ${index + 1}: не указано название`]
      : []),
      ...(!((item.quantity ?? 0) > 0) ?
        [
          `${item.name || `Позиция ${index + 1}`}: количество должно быть больше нуля`,
        ]
      : []),
      ...(item.unitPrice === null || item.unitPrice < 0 ?
        [
          `${item.name || `Позиция ${index + 1}`}: укажите цену (не меньше нуля)`,
        ]
      : []),
    ]),
    ...(conflicts.some(({ key }) => !resolvedConflicts.includes(key)) ?
      [
        `Разрешите конфликты в полях: ${conflicts
          .filter(({ key }) => !resolvedConflicts.includes(key))
          .map(({ label }) => label)
          .join(", ")}`,
      ]
    : []),
  ];
  const canConfirm = validation.length === 0;

  const chooseConflict = (key: keyof CommercialProposal, value: string) => {
    setProposal((current) => ({
      ...current,
      [key]: key === "documentTotal" ? Number(value) : value,
    }));
    setResolvedConflicts((current) =>
      current.includes(key) ? current : [...current, key],
    );
    submissionKey.current = crypto.randomUUID();
    setConfirmed(false);
    setSaved(null);
  };
  const resolveConflictManually = (key: keyof CommercialProposal) => {
    setResolvedConflicts((current) =>
      current.includes(key) ? current : [...current, key],
    );
  };
  const useModelResult = (uploadIndex: number, run: ModelRun) => {
    if (!run.proposal) return;
    const updatedUploads = uploads.map((upload, index) =>
      index === uploadIndex ?
        {
          ...upload,
          proposal: { ...run.proposal!, notes: upload.proposal.notes ?? "" },
        }
      : upload,
    );
    setUploads(updatedUploads);
    setProposal(mergeUploads(updatedUploads));
    setResolvedConflicts([]);
    setConfirmed(false);
    setSaved(null);
    submissionKey.current = crypto.randomUUID();
  };
  const send = async () => {
    setSubmitting(true);
    setError("");
    try {
      const result = await submitProposal(
        {
          proposal,
          sources: uploads
            .filter(({ error: sourceError }) => !sourceError)
            .map(({ file, metadata }) => ({ name: file.name, ...metadata })),
        },
        submissionKey.current,
      );
      setSaved({ id: result.id, createdAt: result.createdAt });
    } catch (cause) {
      setError(
        cause instanceof Error ?
          cause.message
        : "Не удалось сохранить предложение",
      );
    } finally {
      setSubmitting(false);
    }
  };
  const download = (kind: "json" | "csv") => {
    const content =
      kind === "json" ?
        JSON.stringify(
          {
            proposal,
            sources: uploads.map(({ file, metadata }) => ({
              name: file.name,
              metadata,
            })),
            total,
          },
          null,
          2,
        )
      : [
          ["Наименование", "Количество", "Единица", "Цена", "Сумма"],
          ...proposal.items.map((item) => [
            item.name,
            item.quantity,
            item.unit,
            item.unitPrice,
            itemSum(item),
          ]),
        ]
          .map((row) =>
            row
              .map((cell) => {
                const value = String(cell ?? "");
                const safe = /^[\s]*[=+@-]/.test(value) ? `'${value}` : value;
                return `"${safe.replaceAll('"', '""')}"`;
              })
              .join(";"),
          )
          .join("\r\n");
    const blob = new Blob([kind === "csv" ? `\uFEFF${content}` : content], {
      type:
        kind === "csv" ?
          "text/csv;charset=utf-8"
        : "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `proposal.${kind}`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  if (!sessionChecked)
    return (
      <main className="reader">
        <div className="analysis">
          <div className="spinner" />
          <p>Проверяем доступ…</p>
        </div>
      </main>
    );
  if (!authorized && passwordRequired)
    return (
      <main className="reader">
        <header className="reader-header">
          <div>
            <span className="eyebrow">READ DOCUMENT</span>
            <h1>Вход в ReadDocument</h1>
            <p>Введите пароль доступа, заданный для сервера.</p>
          </div>
        </header>
        <section className="reader-content">
          <form className="login-card" onSubmit={(event) => void login(event)}>
            <label>
              Пароль
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </label>
            {authError && (
              <div className="alert" role="alert">
                {authError}
              </div>
            )}
            <button className="primary-cta" type="submit">
              Войти
            </button>
          </form>
        </section>
      </main>
    );

  if (workspace === "annotation") return <AnnotationWorkspace onBack={() => setWorkspace("reader")} />;

  return (
    <main className="reader">
      <header className="reader-header">
        <div>
          <span className="eyebrow">READ DOCUMENT</span>
          <h1>Чтение коммерческого предложения</h1>
          <p>
            Загрузите документы — проверьте извлеченные данные и сохраните их в
            системе.
          </p>
        </div>
        <div className="header-actions">
          <button className="outline-cta" disabled={reading || submitting} onClick={() => setWorkspace("annotation")}>
            Разметка для обучения
          </button>
          {uploads.length > 0 && (
            <button className="outline-cta" onClick={reset}>
              Новое чтение
            </button>
          )}
          {passwordRequired && (
            <button className="outline-cta" onClick={() => void logout()}>
              Выйти
            </button>
          )}
        </div>
      </header>
      <section className="reader-content">
        {error && (
          <div className="alert" role="alert">
            {error}
          </div>
        )}
        {uploads.length === 0 && !reading && (
          <ModelSelector
            models={models}
            selected={selectedModels}
            mode={selectionMode}
            onModeChange={changeSelectionMode}
            onToggle={toggleModel}
          />
        )}
        {uploads.length === 0 && !reading && (
          <div
            className={`dropzone ${dragging ? "dragging" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".docx,.xlsx,.xls,.csv,.tsv,.txt,.json,.pdf,.png,.jpg,.jpeg,.webp"
              onChange={onInput}
            />
            <div className="upload-icon">↑</div>
            <strong>Перетащите документы сюда</strong>
            <p>
              или <u>выберите файлы на компьютере</u>
            </p>
            <small>
              PDF, DOCX, таблицы, текст и изображения · до 25 МБ на файл
            </small>
            <small className="privacy-note">
              Все документы обрабатываются локально. Tesseract читает
              сканы, локальная модель определяет поля и структуру таблиц. Документы и
              результаты не отправляются во внешние сервисы.
            </small>
          </div>
        )}
        {reading && (
          <div className="analysis">
            <div className="spinner" />
            <h2>
              {selectedModels.length > 1 ?
                `Сравниваем ${selectedModels.length} модели`
              : "Читаем документы"}
            </h2>
            <p>
              Готово {readProgress.done} из {readProgress.total}. Документы
              обрабатываются по одному, и результат каждого сохраняется по мере
              завершения.
            </p>
            {readProgress.current && <small className="active-models">Сейчас: {readProgress.current}</small>}
            <small className="active-models">
              {models
                .filter(({ id }) => selectedModels.includes(id))
                .map(({ name }) => name)
                .join(" → ")}
            </small>
          </div>
        )}
        {!reading && uploads.length > 0 && (
          <>
            <div className="results-heading">
              <div>
                <h2>Результаты распознавания</h2>
                <p>
                  {confirmed ?
                    "Данные проверены и готовы к сохранению."
                  : "Проверьте источники, конфликты и поля перед сохранением."}
                </p>
              </div>
              <div className="heading-actions">
                {confirmed && (
                  <span className="confirmed-badge">✓ Проверено</span>
                )}
                <span className="file-count">
                  {uploads.length} {uploads.length === 1 ? "файл" : "файлов"}
                </span>
              </div>
            </div>
            <div className="uploaded-results">
              {uploads.map(({ file, metadata, error: fileError }, index) => (
                <div
                  className="result-row"
                  key={`${file.name}-${file.lastModified}-${index}`}>
                  <button
                    className="source-link"
                    onClick={() =>
                      setPreviewFile(
                        previewFile === `${file.name}-${index}` ? null : (
                          `${file.name}-${index}`
                        ),
                      )
                    }>
                    {file.name}
                  </button>
                  <span className={`status ${fileError || (metadata.outcome && metadata.outcome.state !== "complete") ? "warning" : "done"}`}>
                    {fileError ?
                      fileError
                    : metadata.outcome && metadata.outcome.state !== "complete" ? metadata.outcome.message
                    : `✓ ${metadata.parser}${metadata.ocrPages ? ` · OCR страницы ${metadata.ocrPageNumbers?.join(", ")}` : ""}${metadata.cacheHit ? " · из локального кэша" : ""}`
                    }
                  </span>
                  {!fileError && <EvidenceExplanation metadata={metadata} />}
                  {!!metadata.ocrQuality?.length && (
                    <p className="validation-note">
                      Качество OCR по Tesseract (ориентир, не точность полей): {metadata.ocrQuality.map(({ page, meanConfidence }) =>
                        `стр. ${page}: ${meanConfidence === null ? "не оценено" : `${Math.round(meanConfidence)}%`}`,
                      ).join(" · ")}
                    </p>
                  )}
                  {!!metadata.timingsMs && (
                    <p className="validation-note">
                      Время: чтение {Math.round(metadata.timingsMs.read / 1000)} с · модель {Math.round(metadata.timingsMs.model / 1000)} с · проверка {Math.round(metadata.timingsMs.validation / 1000)} с
                    </p>
                  )}
                  {!!metadata.outcome?.unavailable.length && (
                    <details className="validation-note">
                      <summary>Не удалось извлечь ({metadata.outcome.unavailable.length})</summary>
                      <ul>{metadata.outcome.unavailable.map((entry) => (
                        <li key={entry.field}>{entry.label}</li>
                      ))}</ul>
                    </details>
                  )}
                  {metadata.verification && (
                    <p className="validation-note">
                      {metadata.verification.mode === "model_error" ?
                        "Локальная модель не смогла извлечь данные"
                      : `Повторная проверка: ${metadata.verification.reviewCompleted ? "завершена или не требовалась" : "не завершена"} · спорных строк отправлено модели: ${metadata.verification.targetedReviewRows ?? metadata.verification.unclaimedRows.length} · визуальное чтение: ${metadata.verification.visionUsed ? "выполнено" : "не выполнялось"}`
                      }
                    </p>
                  )}
                </div>
              ))}
            </div>
            {uploads.some(({ metadata }) => metadata.warnings.length > 0) && (
              <section className="validation-box">
                <strong>Предупреждения извлечения</strong>
                {uploads.map(({ file, metadata }) =>
                  metadata.warnings.map((warning, index) => (
                    <p
                      className="validation-note"
                      key={`${file.name}-${index}`}>
                      {file.name}: {warning}
                    </p>
                  )),
                )}
              </section>
            )}
            {previewFile && (
              <SourcePreview
                upload={
                  uploads.find(
                    ({ file }, index) =>
                      `${file.name}-${index}` === previewFile,
                  )!
                }
              />
            )}
            {uploads.some(
              ({ metadata }) => (metadata.modelRuns?.length ?? 0) > 1,
            ) && (
              <ModelComparison
                uploads={uploads}
                currency={proposal.currency}
                onUse={useModelResult}
              />
            )}
            {conflicts.length > 0 && (
              <section className="conflict-panel">
                <h3>Найдены разные значения</h3>
                <p>
                  Выберите источник или сначала исправьте значение в редакторе,
                  затем подтвердите его вручную.
                </p>
                {conflicts.map(({ key, label, values }) => (
                  <div className="conflict-choice" key={key}>
                    <label className="conflict-row">
                      {label}
                      <select
                        value={String(proposal[key] ?? "")}
                        onChange={(event) =>
                          chooseConflict(key, event.target.value)
                        }>
                        {values.map(({ value, file }, index) => (
                          <option value={value} key={`${file}-${index}`}>
                            {value} — {file}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      className="text-button"
                      onClick={() => resolveConflictManually(key)}>
                      Оставить текущее значение
                    </button>
                    {resolvedConflicts.includes(key) && (
                      <small className="resolved-conflict">
                        Выбор подтверждён
                      </small>
                    )}
                  </div>
                ))}
              </section>
            )}
            {editing ?
              <ProposalEditor
                proposal={proposal}
                onChange={(value) => {
                  setProposal(value);
                  setConfirmed(false);
                  setSaved(null);
                  submissionKey.current = crypto.randomUUID();
                }}
                onCancel={() => {
                  setProposal(editOriginal.current);
                  setEditing(false);
                }}
                onConfirm={() => setEditing(false)}
              />
            : <div className="proposal-data">
                {textFields.map(([key, label]) => (
                  <DataField
                    key={key}
                    label={label}
                    value={String(proposal[key] || "Не удалось извлечь")}
                    evidence={uploads.flatMap(({ metadata }) =>
                      asEvidence(metadata.fieldEvidence?.[key]),
                    )}
                  />
                ))}
                <DataField
                  label="Итог по позициям"
                  value={money(total, proposal.currency)}
                />
                {proposal.documentTotal !== null && (
                  <DataField
                    label="Итог из документа"
                    value={money(proposal.documentTotal, proposal.currency)}
                    evidence={uploads.flatMap(({ metadata }) =>
                      asEvidence(metadata.fieldEvidence?.documentTotal),
                    )}
                  />
                )}
                {proposal.items.length > 0 && (
                  <div className="items-data">
                    <h3>Позиции</h3>
                    {proposal.items.map((item, index) => (
                      <div
                        className={`item-data ${duplicateItems.includes(item) ? "duplicate-item" : ""}`}
                        key={`${item.name}-${index}`}>
                        <span>
                          {item.name || "Без названия"}
                          {duplicateItems.includes(item) && (
                            <small> Возможный дубль из нескольких файлов</small>
                          )}
                        </span>
                        <span>
                          {item.quantity ?? "Количество: не удалось извлечь"} {item.unit}
                        </span>
                        <strong>
                          {money(itemSum(item), proposal.currency)}
                        </strong>
                        <details>
                          <summary>Источники значений</summary>
                          {(item.components ?? []).map((component, ci) => (
                            <DataField key={`cost-${ci}`} label={component.label}
                              value={`Цена ${money(component.unitPrice, proposal.currency)} · сумма ${money(component.lineTotal, proposal.currency)}`} />
                          ))}
                          {(item.additionalFields ?? []).map((field, fi) => (
                            <DataField key={`extra-${fi}`} label={field.label} value={field.value} />
                          ))}
                          {itemEvidence(item).map((e, i) => (
                            <DataField
                              key={i}
                              label="Значение в исходном документе"
                              value={String(e.value ?? e.excerpt)}
                              evidence={[e]}
                            />
                          ))}
                          <small>
                            Для изменённых вручную позиций исходные цитаты
                            доступны в просмотре файла.
                          </small>
                        </details>
                      </div>
                    ))}
                    <div className="total-data">
                      <span>Итого</span>
                      <strong>{money(total, proposal.currency)}</strong>
                    </div>
                    {total !== null &&
                      proposal.documentTotal !== null &&
                      Math.abs((total ?? 0) - proposal.documentTotal) >
                        0.01 && (
                        <p className="validation-note">
                          Сумма позиций отличается от суммы в документе на{" "}
                          {money(
                            Math.abs((total ?? 0) - proposal.documentTotal),
                            proposal.currency,
                          )}
                          . Проверьте НДС, скидку и доставку.
                        </p>
                      )}
                  </div>
                )}
                {(proposal.additionalFields ?? []).map((field, index) => (
                  <DataField
                    key={`extra-${index}`}
                    label={field.label}
                    value={field.value}
                  />
                ))}
                {proposal.notes && (
                  <DataField
                    label="Извлеченный текст документа"
                    value={proposal.notes}
                    multiline
                  />
                )}
                <div className="validation-box">
                  <strong>
                    {canConfirm ?
                      "Обязательные данные заполнены"
                    : "Что нужно проверить"}
                  </strong>
                  {validation.length > 0 && (
                    <ul>
                      {validation.map((message) => (
                        <li key={message}>{message}</li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="data-actions">
                  <button
                    className="outline-cta"
                    onClick={() => download("json")}>
                    Скачать JSON
                  </button>
                  <button
                    className="outline-cta"
                    onClick={() => download("csv")}>
                    Скачать CSV
                  </button>
                  <button
                    className="outline-cta"
                    onClick={() => {
                      editOriginal.current = proposal;
                      setEditing(true);
                    }}>
                    Изменить данные
                  </button>
                  {!confirmed && (
                    <button
                      className="primary-cta"
                      disabled={!canConfirm}
                      onClick={() => setConfirmed(true)}>
                      Подтвердить данные
                    </button>
                  )}
                  {confirmed && !saved && (
                    <button
                      className="primary-cta"
                      disabled={submitting}
                      onClick={() => void send()}>
                      {submitting ? "Сохраняем…" : "Сохранить на сервере"}
                    </button>
                  )}
                </div>
                {saved && (
                  <div className="saved-message" role="status">
                    Предложение сохранено · № {saved.id} ·{" "}
                    {new Date(saved.createdAt).toLocaleString("ru-RU")}
                  </div>
                )}
              </div>
            }
          </>
        )}
      </section>
    </main>
  );
}

function ModelSelector({
  models,
  selected,
  mode,
  onModeChange,
  onToggle,
}: {
  models: ExtractionModel[];
  selected: string[];
  mode: "single" | "compare";
  onModeChange: (mode: "single" | "compare") => void;
  onToggle: (model: string) => void;
}) {
  return (
    <section className="model-selector">
      <div className="model-selector-heading">
        <div>
          <span className="step-label">ШАГ 1</span>
          <h2>Модель распознавания</h2>
          <p>
            Модель определяет поля и колонки и проверяет их по источнику.
            Если модель недоступна, резервный разбор сопровождается
            предупреждение.
          </p>
        </div>
        {models.length > 1 && (
          <div
            className="mode-switch"
            role="group"
            aria-label="Режим распознавания">
            <button
              type="button"
              className={mode === "single" ? "active" : ""}
              onClick={() => onModeChange("single")}>
              Одна модель
            </button>
            <button
              type="button"
              className={mode === "compare" ? "active" : ""}
              onClick={() => onModeChange("compare")}>
              Сравнить модели
            </button>
          </div>
        )}
      </div>
      <div className="model-grid">
        {models.map((model) => {
          const checked = selected.includes(model.id);
          return (
            <button
              type="button"
              className={`model-card ${checked ? "selected" : ""}`}
              onClick={() => onToggle(model.id)}
              aria-pressed={checked}
              key={model.id}>
              <span className="model-check">{checked ? "✓" : ""}</span>
              <span className="model-title">
                {model.name}
                {model.recommended && <small>Рекомендуемая</small>}
              </span>
              <span className="model-description">{model.description}</span>
              <span className="model-meta">
                {model.size}
                {model.installed === true ?
                  " · загружена"
                : model.installed === false ?
                  " · не загружена"
                : ""}
              </span>
            </button>
          );
        })}
      </div>
      <p className="model-selection-note">
        {mode === "compare" ?
          `Выбрано ${selected.length} модели. Они запускаются по очереди, поэтому сравнение занимает больше времени.`
        : "Будет использована одна серверная модель."}
      </p>
    </section>
  );
}

function ModelComparison({
  uploads,
  currency,
  onUse,
}: {
  uploads: Upload[];
  currency: string;
  onUse: (uploadIndex: number, run: ModelRun) => void;
}) {
  return (
    <section className="model-comparison">
      <h3>Сравнение моделей</h3>
      <p>
        Итог собран по совпадающим полям. Для позиций взят наиболее полный
        результат; при необходимости выберите результат конкретной модели.
      </p>
      {uploads.map((upload, uploadIndex) => {
        const runs = upload.metadata.modelRuns ?? [];
        if (runs.length < 2) return null;
        return (
          <div
            className="model-file"
            key={`${upload.file.name}-${uploadIndex}`}>
            <strong>{upload.file.name}</strong>
            <div className="model-run-grid">
              {runs.map((run) => {
                const items = run.proposal?.items ?? [];
                const runTotal = sumItems(items);
                const fields =
                  run.proposal ?
                    textFields.filter(([key]) => Boolean(run.proposal?.[key]))
                      .length
                  : 0;
                return (
                  <article
                    className={`model-run ${run.status}`}
                    key={run.model}>
                    <div>
                      <b>{run.name}</b>
                      <span>{(run.durationMs / 1000).toFixed(1)} сек.</span>
                    </div>
                    {run.status === "parsed" ?
                      <>
                        <p>
                          {fields} полей · {items.length} позиций · уверенность{" "}
                          {Math.round(run.confidence * 100)}%
                        </p>
                        <strong>
                          {money(
                            runTotal,
                            currency || String(run.proposal?.currency ?? ""),
                          )}
                        </strong>
                        <button
                          type="button"
                          className="text-button"
                          onClick={() => onUse(uploadIndex, run)}>
                          Использовать этот результат
                        </button>
                      </>
                    : <p className="model-error">
                        {run.error || "Нет результата"}
                      </p>
                    }
                  </article>
                );
              })}
            </div>
          </div>
        );
      })}
    </section>
  );
}

function SourcePreview({ upload }: { upload: Upload }) {
  const previewable =
    upload.file.type === "application/pdf" ||
    upload.file.type.startsWith("image/");
  const url = useMemo(
    () => (previewable ? URL.createObjectURL(upload.file) : ""),
    [previewable, upload.file],
  );
  useEffect(
    () => () => {
      if (url) URL.revokeObjectURL(url);
    },
    [url],
  );
  return (
    <section className="source-preview">
      <div>
        <strong>{upload.file.name}</strong>
        <span>
          {(upload.file.size / 1024 / 1024).toFixed(2)} МБ ·{" "}
          {upload.metadata.parser}
        </span>
      </div>
      {url &&
        (upload.file.type === "application/pdf" ?
          <iframe
            className="document-frame"
            title={`Просмотр ${upload.file.name}`}
            src={url}
          />
        : <img
            className="document-image"
            src={url}
            alt={`Предпросмотр ${upload.file.name}`}
          />)}
      {upload.proposal.notes && <pre>{upload.proposal.notes}</pre>}
      {!upload.proposal.notes && !previewable && (
        <pre>
          {upload.error ||
            "Текстовый слой недоступен; обработано с помощью OCR."}
        </pre>
      )}
      {upload.metadata.warnings.map((warning) => (
        <p className="validation-note" key={warning}>
          {warning}
        </p>
      ))}
    </section>
  );
}

function ProposalEditor({
  proposal,
  onChange,
  onCancel,
  onConfirm,
}: {
  proposal: CommercialProposal;
  onChange: (proposal: CommercialProposal) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const update = <K extends keyof CommercialProposal>(
    key: K,
    value: CommercialProposal[K],
  ) => onChange({ ...proposal, [key]: value });
  const updateItem = (
    index: number,
    key: "name" | "quantity" | "unit" | "unitPrice",
    value: string,
  ) => {
    const items = proposal.items.map((item, itemIndex) =>
      itemIndex === index ?
        {
          ...item,
          ...(key === "quantity" || key === "unitPrice" ?
            { lineTotal: null, components: [] }
          : {}),
          [key]:
            key === "name" || key === "unit" ? value
            : value === "" ? null
            : Number(value),
        }
      : item,
    );
    update("items", items);
  };
  return (
    <div className="proposal-editor">
      <div className="editor-grid">
        {textFields.map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              value={String(proposal[key] || "")}
              onChange={(event) => update(key, event.target.value as never)}
            />
          </label>
        ))}
      </div>
      <div className="editor-items-heading">
        <h3>Позиции</h3>
        <button
          className="text-button"
          onClick={() =>
            update("items", [
              ...proposal.items,
              { name: "", quantity: null, unit: "", unitPrice: null },
            ])
          }>
          ＋ Добавить
        </button>
      </div>
      {proposal.items.map((item, index) => (
        <div className="editor-item" key={index}>
          <input
            aria-label="Наименование"
            placeholder="Наименование"
            value={item.name}
            onChange={(event) => updateItem(index, "name", event.target.value)}
          />
          <input
            aria-label="Количество"
            placeholder="Кол-во"
            type="number"
            min="0"
            step="any"
            value={item.quantity ?? ""}
            onChange={(event) =>
              updateItem(index, "quantity", event.target.value)
            }
          />
          <input
            aria-label="Единица измерения"
            placeholder="Ед."
            value={item.unit}
            onChange={(event) => updateItem(index, "unit", event.target.value)}
          />
          <input
            aria-label="Цена за единицу"
            placeholder="Цена за ед."
            type="number"
            min="0"
            step="any"
            value={item.unitPrice ?? ""}
            onChange={(event) =>
              updateItem(index, "unitPrice", event.target.value)
            }
          />
          <button
            className="remove-item"
            aria-label="Удалить позицию"
            onClick={() =>
              update(
                "items",
                proposal.items.filter((_, itemIndex) => itemIndex !== index),
              )
            }>
            ×
          </button>
        </div>
      ))}
      <label className="notes-label">
        Текст документа
        <textarea
          rows={6}
          value={proposal.notes}
          onChange={(event) => update("notes", event.target.value)}
        />
      </label>
      <label className="notes-label">
        Итог из документа
        <input
          type="number"
          min="0"
          step="any"
          value={proposal.documentTotal ?? ""}
          onChange={(event) =>
            update(
              "documentTotal",
              event.target.value === "" ? null : Number(event.target.value),
            )
          }
        />
      </label>
      <div className="data-actions">
        <button className="outline-cta" onClick={onCancel}>
          Отмена
        </button>
        <button className="primary-cta" onClick={onConfirm}>
          Сохранить изменения
        </button>
      </div>
    </div>
  );
}

function DataField({
  label,
  value,
  multiline = false,
  evidence = [],
}: {
  label: string;
  value: string;
  multiline?: boolean;
  evidence?: FieldEvidence[];
}) {
  return (
    <div className={`data-field ${multiline ? "multiline" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {evidence.map((source, index) => (
        <small
          className={
            source.verifiedInSource ? "evidence" : "evidence unverified"
          }
          key={`${source.file}-${index}`}>
          {source.file}
          {source.page ? ` · стр. ${source.page}` : ""}
          {source.sheet ? ` · лист ${source.sheet}` : ""}
          {source.row ? ` · строка ${source.row}, ячейка ${source.cell}` : ""}
          {source.method ? ` · ${source.method}` : ""}
          {source.confidence !== undefined ?
            ` · ${Math.round(source.confidence * 100)}%`
          : ""}
          : «{source.excerpt}»{source.warning ? ` · ${source.warning}` : ""}
          {source.verifiedInSource ? "" : " · точное совпадение не найдено"}
        </small>
      ))}
    </div>
  );
}

export default App;
