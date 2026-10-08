import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, FormEvent } from "react";
import {
  canUseExtractionApi,
  extractionFileAccept,
  getExtractionModels,
  parseWithExtractionApi,
  submitProposal,
} from "./lib/extractionApi";
import type { ExtractionModel } from "./lib/extractionApi";
import type {
  CommercialProposal,
  ExtractionMetadata,
  FieldEvidence,
} from "./lib/documentParser";
import "./App.css";
import AnnotationWorkspace from "./AnnotationWorkspace";
import { sourceMethodLabel } from "./lib/sourceEvidence";

type Upload = {
  file: File;
  proposal: Partial<CommercialProposal>;
  metadata: ExtractionMetadata;
  error?: string;
};

function ExtractionNotices({ uploads }: { uploads: Upload[] }) {
  const groups = new Map<string, Set<string>>();
  for (const { file, metadata } of uploads) {
    for (const warning of metadata.warnings) {
      const message = /ConnectError|не удалось связаться|UNEXPECTED_EOF/i.test(warning)
        ? "Нет соединения с сервисом распознавания. Проверьте подключение и повторите загрузку."
        : warning.replace(/\((?:ConnectTimeout|ReadTimeout|TimeoutException)\)/g, "").trim();
      const files = groups.get(message) ?? new Set<string>();
      files.add(file.name);
      groups.set(message, files);
    }
  }
  if (!groups.size) return null;
  const notices = Array.from(groups);
  return <section className="extraction-notices" aria-label="Замечания к обработке">
    <strong>Требует внимания</strong>
    <p className="notice-primary">{notices[0][0]}</p>
    <details>
      <summary>Подробнее о документах{notices.length > 1 ? ` · ${notices.length} замечаний` : ""}</summary>
      <ul>{notices.map(([message, files]) => <li key={message}>
        {notices.length > 1 && <p>{message}</p>}
        <span>{Array.from(files).join(", ")}</span>
      </li>)}</ul>
    </details>
  </section>;
}
function UsageDetails({ metadata }: { metadata: ExtractionMetadata }) {
  const usage = metadata.apiUsage;
  if (!usage) return <p className="validation-note">Расход API: статистика недоступна</p>;
  const money = (value: number | null) => value === null ? "не определена" : `$${value.toFixed(6)}`;
  return <details className="validation-note">
    <summary>{metadata.cacheHit ? "Из кеша · новых токенов: 0 · стоимость сейчас: $0" :
      `Токены: ${(usage.inputTokens + usage.outputTokens).toLocaleString("ru-RU")} · стоимость API ≈ ${money(usage.estimatedCostUsd)}`}</summary>
    <p>{metadata.cacheHit ? "Первоначальная обработка: " : ""}Вход: {usage.inputTokens.toLocaleString("ru-RU")} · из них кеш API: {usage.cachedInputTokens.toLocaleString("ru-RU")} · выход: {usage.outputTokens.toLocaleString("ru-RU")} · из них рассуждения: {usage.reasoningTokens.toLocaleString("ru-RU")}</p>
    <p>Стоимость обработки ≈ {money(usage.estimatedCostUsd)} · тариф от {usage.pricingDate}, USD, без налогов и сервера.</p>
    {!usage.complete && <p>Учёт неполный: API не сообщил весь расход или тариф модели неизвестен. Итоговая стоимость не определена.</p>}
    {(["vision", "semantic"] as const).map(stage => {
      const calls = usage.calls.filter(call => call.stage === stage);
      if (!calls.length) return null;
      const cost = calls.every(call => call.estimatedCostUsd !== null) ? calls.reduce((sum, call) => sum + (call.estimatedCostUsd ?? 0), 0) : null;
      return <p key={stage}>{stage === "vision" ? "Чтение изображений" : "Разбор структуры"}: {calls.length} запросов · {calls.reduce((sum, call) => sum + call.inputTokens + call.outputTokens, 0).toLocaleString("ru-RU")} токенов · ≈ {money(cost)} · {Array.from(new Set(calls.map(call => call.model))).join(", ")}</p>;
    })}
  </details>;
}

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
        Проценты — оценка надёжности по правилам проверки источника, структуры и
        расчётов. Это эвристика backend, а не измеренная точность распознавания.
      </p>
      <p>
        Перед сохранением проверьте смысл поля, конфликты и суммы по
        оригиналу.
      </p>
    </details>
  );
}
const builtInModels: ExtractionModel[] = [
  {
    id: "openai",
    name: "Модель",
    description: "Чтение документов, изображений и сканов с привязкой значений к источнику",
    size: "Модель задаётся в настройках сервера",
    recommended: true,
  },
];
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
  if (value === null) return "Не указано в КП";

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
  !items.length || items.some((item) => itemSum(item) === null) ? null : (
    items.reduce((sum, item) => sum + (itemSum(item) ?? 0), 0)
  );
const asEvidence = (
  value: FieldEvidence | FieldEvidence[] | undefined,
): FieldEvidence[] =>
  !value ? []
  : Array.isArray(value) ? value
  : [value];
const unavailableLabel = (reason?: "absent" | "unreadable" | "unverified") =>
  reason === "absent" ? "Не указано в КП"
  : reason === "unreadable" ? "Не удалось прочитать"
  : "Требуется проверка";

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
  const [modelsError, setModelsError] = useState("");
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
        if (!active) return;
        const openai = availableModels.find((model) => model.id === "openai");
        if (!openai) {
          setModelsError("Сервис распознавания недоступен. Обновите сервер.");
          return;
        }
        setModels([openai]);
        setModelsError("");
      })
      .catch((cause) => {
        if (active) setModelsError(cause instanceof Error ? cause.message : "Не удалось проверить настройки распознавания");
      });
    return () => {
      active = false;
    };
  }, [authorized, sessionChecked]);
  const total = useMemo(() => sumItems(proposal.items), [proposal.items]);

  const missingValue = (field: string) => {
    const successful = uploads.filter((upload) => !upload.error);
    if (!successful.length) return "Не удалось извлечь данные";
    if (uploads.some((upload) => upload.error))
      return "Не удалось проверить во всех файлах";
    const reasons = successful.flatMap(({ metadata }) =>
      (metadata.outcome?.unavailable ?? [])
        .filter((entry) => entry.field === field)
        .map((entry) => entry.reason),
    );
    if (reasons.includes("unreadable")) return "Не удалось прочитать";
    if (reasons.some((reason) => reason !== "absent")) return "Требуется проверка";
    return "Не указано в КП";
  };
  const itemMissingValue = (item: CommercialProposal["items"][number], field: string) => {
    for (const upload of uploads.filter((source) => !source.error)) {
      const index = (upload.proposal.items ?? []).indexOf(item);
      if (index < 0) continue;
      const entry = upload.metadata.outcome?.unavailable.find(
        (candidate) => candidate.field === `items.${index}.${field}`,
      );
      if (entry) return unavailableLabel(entry.reason);
      return upload.metadata.verification?.reviewCompleted ? "Не указано в КП" : "Требуется проверка";
    }
    return "Требуется проверка";
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
      const results: Array<Upload | undefined> = new Array(acceptable.length);
      const activeFiles = new Map<number, string>();
      let nextIndex = 0;
      let done = 0;
      const readFile = async (file: File, sizeError: string): Promise<Upload> => {
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
            const result = await parseWithExtractionApi(file, ["openai"]);
            return {
              file,
              proposal: result.proposal,
              metadata: result.metadata,
              error:
                (
                  result.metadata.status === "error" ||
                  result.metadata.status === "unsupported" ||
                  (result.metadata.status === "empty" &&
                    (!result.metadata.outcome || result.metadata.outcome.unavailable.some((entry) => entry.reason !== "absent")))
                ) ?
                  result.metadata.warnings[0] || "Не удалось прочитать документ"
                : undefined,
            };
          } catch (cause) {
            return {
              file,
              proposal: {},
              metadata: {
                sourceName: file.name,
                parser: "Распознавание документа",
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
      };
      const worker = async () => {
        while (nextIndex < acceptable.length) {
          const index = nextIndex++;
          const { file, error: sizeError } = acceptable[index];
          activeFiles.set(index, file.name);
          setReadProgress({ done, total: acceptable.length, current: [...activeFiles.values()].join(" · ") });
          results[index] = await readFile(file, sizeError);
          activeFiles.delete(index);
          done += 1;
          setUploads(results.filter((upload): upload is Upload => upload !== undefined));
          setReadProgress({ done, total: acceptable.length, current: [...activeFiles.values()].join(" · ") });
        }
      };
      await Promise.all(Array.from({ length: Math.min(2, acceptable.length) }, worker));
      const successful = results.filter((upload): upload is Upload => !!upload && !upload.error);
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
            original.unit === item.unit &&
            original.lineTotal === item.lineTotal &&
            JSON.stringify(original.components ?? []) === JSON.stringify(item.components ?? [])
          ) ?
            Object.entries(upload.metadata.fieldEvidence ?? {})
              .filter(([key]) => key.startsWith(`items.${index}.`))
              .flatMap(([, value]) => asEvidence(value))
          : [],
        ),
      );
  const additionalEvidence = (field: { label: string; value: string }) =>
    uploads.filter((upload) => !upload.error).flatMap((upload) =>
      (upload.proposal.additionalFields ?? []).flatMap((original, index) =>
        original.label === field.label && original.value === field.value ?
          asEvidence(upload.metadata.fieldEvidence?.[`additionalFields.${index}.value`])
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
    ...(!proposal.items.length ? ["Добавьте хотя бы одну позицию"] : []),
    ...proposal.items.flatMap((item, index) => [
      ...(!item.name.trim() ?
        [`Позиция ${index + 1}: не указано название`]
      : []),
      ...(item.quantity !== null && (!Number.isFinite(item.quantity) || item.quantity < 0) ?
        [
          `${item.name || `Позиция ${index + 1}`}: количество должно быть не меньше нуля`,
        ]
      : []),
      ...(item.unitPrice !== null && (!Number.isFinite(item.unitPrice) || item.unitPrice < 0) ?
        [
          `${item.name || `Позиция ${index + 1}`}: цена должна быть не меньше нуля`,
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
            Загрузите документы и изображения. Проверьте данные по
            оригиналу и сохраните результат.
          </p>
        </div>
        <div className="header-actions">
          <button className="outline-cta" disabled={reading || submitting} onClick={() => setWorkspace("annotation")}>
            Проверка документов
          </button>
          {uploads.length > 0 && (
            <button className="outline-cta" disabled={reading || submitting} onClick={reset}>
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
          <OpenAIProvider model={models[0]} error={modelsError} />
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
              accept={extractionFileAccept}
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
              Документы и изображения отправляются внешнему API для обработки.
              Отсутствующие данные отмечаются «Не указано в КП»; ошибки чтения
              показываются отдельно. Ключ API хранится на сервере.
            </small>
          </div>
        )}
        {reading && (
          <div className="analysis">
            <div className="spinner" />
            <h2>Читаем документы</h2>
            <p>
              Готово {readProgress.done} из {readProgress.total}. До двух файлов
              обрабатываются одновременно; результат каждого сохраняется по мере
              завершения.
            </p>
            {readProgress.current && <small className="active-models">Сейчас: {readProgress.current}</small>}
            <small className="active-models">
              {models[0].name}
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
                    : `✓ ${metadata.parser}${metadata.ocrPages ? ` · прочитаны страницы ${metadata.ocrPageNumbers?.join(", ")}` : ""}${metadata.cacheHit ? " · из кэша сервера" : ""}`
                    }
                  </span>
                  {!fileError && <EvidenceExplanation metadata={metadata} />}
                  {metadata.routing && (
                    <p className="validation-note">
                      Структура документа: {metadata.routing.used ? "проверена визуально" : "прочитана из файла"}
                      {metadata.routing.processed_pages?.length ? ` · страницы ${metadata.routing.processed_pages.join(", ")}` : ""}
                      {metadata.routing.mandatory && !metadata.routing.used ? " · требуется визуальная проверка" : ""}
                    </p>
                  )}
                  <UsageDetails metadata={metadata} />
                  {!!metadata.timingsMs && (
                    <p className="validation-note">
                      Время: чтение {Math.round(metadata.timingsMs.read / 1000)} с · модель {Math.round(metadata.timingsMs.model / 1000)} с · проверка {Math.round(metadata.timingsMs.validation / 1000)} с
                    </p>
                  )}
                  {!!metadata.outcome?.unavailable.length && (
                    <details className="validation-note">
                      <summary>Поля без подтверждённого значения ({metadata.outcome.unavailable.length})</summary>
                      <ul>{metadata.outcome.unavailable.map((entry) => (
                        <li key={entry.field}>{entry.label}: {unavailableLabel(entry.reason)}</li>
                      ))}</ul>
                    </details>
                  )}
                  {metadata.verification && (
                    <p className="validation-note">
                      {metadata.verification.mode === "model_error" ?
                        "Не удалось извлечь данные"
                      : `Охват товарных строк: ${metadata.verification.coverageComplete ? "все строки учтены" : `не проверено строк: ${metadata.verification.unclaimedRows.length}`} · визуальное чтение: ${metadata.verification.visionUsed ? "выполнено" : "не выполнялось"}`
                      }
                    </p>
                  )}
                </div>
              ))}
            </div>
            <ExtractionNotices uploads={uploads} />
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
                    value={String(proposal[key] || missingValue(key))}
                    rawValue={typeof proposal[key] === "string" ? proposal[key] : undefined}
                    evidence={uploads.flatMap(({ metadata }) =>
                      asEvidence(metadata.fieldEvidence?.[key]),
                    )}
                  />
                ))}
                <DataField
                  label="Итог по позициям"
                  value={total === null ? "Недостаточно данных для расчёта" : money(total, proposal.currency)}
                />
                <DataField
                    label="Итог из документа"
                    value={proposal.documentTotal === null ? missingValue("documentTotal") : money(proposal.documentTotal, proposal.currency)}
                    rawValue={proposal.documentTotal ?? undefined}
                    evidence={uploads.flatMap(({ metadata }) =>
                      asEvidence(metadata.fieldEvidence?.documentTotal),
                    )}
                  />
                {proposal.items.length > 0 && (
                  <div className="items-data">
                    <h3>Позиции</h3>
                    {proposal.items.map((item, index) => (
                      <div
                        className={`item-data ${duplicateItems.includes(item) ? "duplicate-item" : ""}`}
                        key={`${item.name}-${index}`}>
                        <span>
                          {item.name || itemMissingValue(item, "name")}
                          {duplicateItems.includes(item) && (
                            <small> Возможный дубль из нескольких файлов</small>
                          )}
                        </span>
                        <span>
                          {item.quantity === null ? `Количество: ${itemMissingValue(item, "quantity")}` : `${item.quantity} ${item.unit}`}
                          {!item.unit && <small className="item-missing">Единица: {itemMissingValue(item, "unit")}</small>}
                          <small className="item-missing">Цена за единицу: {item.unitPrice === null ? itemMissingValue(item, "unitPrice") : money(item.unitPrice, proposal.currency)}</small>
                        </span>
                        <strong>
                          {itemSum(item) === null && !!item.components?.length ?
                            "Стоимость по компонентам"
                          : itemSum(item) === null ? itemMissingValue(item, "lineTotal")
                          : money(itemSum(item), proposal.currency)}
                        </strong>
                        {!!item.components?.length && (
                          <div className="item-costs">
                            <p>{item.componentMode === "alternative" ? "Альтернативные варианты стоимости" : "Составляющие стоимости"}</p>
                            <div className="item-cost-grid">
                              {item.components.map((component, componentIndex) => (
                                <div className="item-cost" key={`${component.label}-${componentIndex}`}>
                                  <span>{component.label}</span>
                                  {component.unitPrice !== null && <span>Цена за единицу: {money(component.unitPrice, proposal.currency)}</span>}
                                  {component.lineTotal !== null && <strong>Сумма: {money(component.lineTotal, proposal.currency)}</strong>}
                                  {component.unitPrice === null && component.lineTotal === null && <span>Стоимость: {itemMissingValue(item, `components.${componentIndex}.lineTotal`)}</span>}
                                </div>
                              ))}
                            </div>
                            {item.componentMode === "alternative" && <small>Варианты сохраняются отдельно и не складываются.</small>}
                          </div>
                        )}
                        <details>
                          <summary>Источники значений</summary>
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
                      <strong>{total === null ? "Недостаточно данных для расчёта" : money(total, proposal.currency)}</strong>
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
                {proposal.items.length === 0 && (
                  <div className="items-data">
                    <h3>Позиции</h3>
                    <p className="validation-note">{missingValue("items")}</p>
                  </div>
                )}
                {(proposal.additionalFields ?? []).map((field, index) => (
                  <DataField
                    key={`extra-${index}`}
                    label={field.label}
                    value={field.value}
                    evidence={additionalEvidence(field)}
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
                      "Данные готовы к проверке и сохранению"
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

function OpenAIProvider({ model, error }: { model: ExtractionModel; error: string }) {
  const configured = model.configured ?? model.installed;
  return (
    <section className="model-selector">
      <div className="model-selector-heading">
        <div>
          <span className="step-label">ШАГ 1</span>
          <h2>Модель распознавания</h2>
          <p>
            Читаем текст, определяем поля и строки таблиц.
            Проверяем значения по источнику. Если обработка недоступна,
            показываем ошибку и предлагаем повторить чтение.
          </p>
        </div>
      </div>
      <div className={`model-card provider-card ${configured === false ? "unavailable" : "selected"}`}>
        <span className="model-check">{configured === false ? "!" : "✓"}</span>
        <span className="model-title">{model.name}</span>
        <span className="model-description">{model.description}</span>
        <span className="model-meta">
          {model.size} · {configured === true ? "API настроен" : configured === false ? "API не настроен" : "Проверяем настройки API"}
        </span>
      </div>
      {error && <p className="model-selection-note" role="alert">{error}</p>}
      {configured === false && (
        <p className="model-selection-note" role="alert">
          Настройте ключ API на сервере и перезапустите сервис.
          Чтение документов недоступно, пока API не настроен.
        </p>
      )}
    </section>
  );
}

function SourcePreview({ upload }: { upload: Upload }) {
  const pdf = upload.file.type === "application/pdf" || /\.pdf$/i.test(upload.file.name);
  const image = upload.file.type.startsWith("image/") || /\.(png|jpe?g|webp|bmp|tiff?)$/i.test(upload.file.name);
  const tiff = /\.tiff?$/i.test(upload.file.name);
  const previewable =
    pdf || image;
  const [preview, setPreview] = useState<{ file: File; url: string } | null>(null);
  const url = preview?.file === upload.file ? preview.url : "";
  useEffect(() => {
    if (!previewable) return;
    const nextUrl = URL.createObjectURL(upload.file);
    let active = true;
    // Ignore the cancelled setup when React checks effect cleanup in StrictMode.
    queueMicrotask(() => {
      if (active) setPreview({ file: upload.file, url: nextUrl });
    });
    return () => {
      active = false;
      URL.revokeObjectURL(nextUrl);
    };
  }, [previewable, upload.file]);
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
        (pdf ?
          <iframe
            className="document-frame"
            title={`Просмотр ${upload.file.name}`}
            src={url}
          />
        : !tiff && <img
            className="document-image"
            src={url}
            alt={`Предпросмотр ${upload.file.name}`}
          />)}
      {tiff && <p className="validation-note">Предпросмотр TIFF доступен не во всех браузерах. Проверьте оригинал изображения и прочитанный текст.</p>}
      {upload.proposal.notes && <pre>{upload.proposal.notes}</pre>}
      {!upload.proposal.notes && !previewable && (
        <pre>
          {upload.error ||
            "Текст документа недоступен. Проверьте предупреждения извлечения."}
        </pre>
      )}
      <ExtractionNotices uploads={[upload]} />
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
            { lineTotal: null }
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
  rawValue,
  multiline = false,
  evidence = [],
}: {
  label: string;
  value: string;
  rawValue?: string | number;
  multiline?: boolean;
  evidence?: FieldEvidence[];
}) {
  return (
    <div className={`data-field ${multiline ? "multiline" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {rawValue !== undefined && rawValue !== "" && evidence.length > 0 &&
        !evidence.some((source) => String(source.value ?? "") === String(rawValue)) && (
          <small className="evidence unverified">
            Значение изменено вручную. Ниже приведены исходные цитаты для сверки.
          </small>
        )}
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
          {(source.sourceMethod || source.source_method || source.method) ? ` · ${sourceMethodLabel(source.sourceMethod || source.source_method || source.method)}` : ""}
          {source.method?.endsWith("+model") && (source.sourceMethod || source.source_method) ? " · смысл определён моделью" : ""}
          {source.sourceId ? ` · источник ${source.sourceId}` : ""}
          {source.bbox ? ` · область [${source.bbox.map((coordinate) => Math.round(coordinate)).join(", ")}]` : ""}
          {source.confidence !== undefined ?
            ` · оценка надёжности ${Math.round(source.confidence * 100)}%`
          : ""}
          {source.visionAgreement === false || source.vision_agreement === false ? " · визуальный текст отличается" : ""}
          : «{source.excerpt}»{source.warning ? ` · ${source.warning}` : ""}
          {source.verifiedInSource ? "" : " · точное совпадение не найдено"}
        </small>
      ))}
    </div>
  );
}

export default App;
