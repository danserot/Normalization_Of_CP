import { useEffect, useMemo, useRef, useState } from "react";
import {
  exportAnnotations,
  getAnnotation,
  listAnnotations,
  saveAnnotation,
  uploadAnnotation,
} from "./lib/annotationApi";
import type {
  AnnotationDocument,
  AnnotationSummary,
  MarkedField,
  MarkedTable,
  SourceCell,
} from "./lib/annotationApi";
import "./AnnotationWorkspace.css";

const labels: Record<string, string> = {
  title: "Название КП",
  client: "Покупатель",
  clientContact: "Контакт покупателя",
  validUntil: "Срок действия",
  supplier: "Поставщик",
  currency: "Валюта",
  vat: "НДС",
  discount: "Скидка",
  delivery: "Стоимость доставки",
  paymentTerms: "Условия оплаты",
  deliveryTerms: "Срок поставки",
  warranty: "Гарантия",
  documentNumber: "Номер документа",
  documentDate: "Дата документа",
  documentTotal: "Общий итог",
};
const formats =
  ".pdf,.docx,.xlsx,.xls,.csv,.tsv,.txt,.json,.png,.jpg,.jpeg,.webp";
const message = (cause: unknown) =>
  cause instanceof Error ? cause.message : "Не удалось выполнить действие";
const fieldOrder = [
  "supplier",
  "client",
  "documentNumber",
  "documentDate",
  "title",
  "clientContact",
  "paymentTerms",
  "deliveryTerms",
  "validUntil",
  "currency",
  "vat",
  "discount",
  "delivery",
  "warranty",
  "documentTotal",
];
const questions: Record<string, string> = {
  supplier: "Кто продаёт товары или услуги?",
  client: "Кому адресовано предложение?",
  documentNumber: "Какой номер указан у этого КП?",
  documentDate: "Когда составлено предложение?",
  title: "Как называется документ?",
  clientContact: "Кто контактное лицо покупателя?",
  paymentTerms: "Как нужно оплатить заказ?",
  deliveryTerms: "Когда или на каких условиях поставят заказ?",
  validUntil: "До какой даты действует предложение?",
  currency: "В какой валюте указаны цены?",
  vat: "Что сказано про НДС?",
  discount: "Указана ли скидка?",
  delivery: "Сколько стоит доставка?",
  warranty: "Какая гарантия указана?",
  documentTotal: "Какова общая сумма всего КП?",
};

function fieldSource(
  cells: SourceCell[],
  field: MarkedField,
): SourceCell | undefined {
  const value = field.value.trim();
  if (!value) return undefined;
  const selected = cells.find(
    (cell) => cell.id === field.cell && cell.text.includes(value),
  );
  if (selected) return selected;
  const matches = cells.filter((cell) => cell.text.includes(value));
  return matches.length === 1 ? matches[0] : undefined;
}

export default function AnnotationWorkspace({
  onBack,
}: {
  onBack: () => void;
}) {
  const [documents, setDocuments] = useState<AnnotationSummary[]>([]);
  const [doc, setDoc] = useState<AnnotationDocument | null>(null);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [progress, setProgress] = useState("");
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [binding, setBinding] = useState("supplier");
  const [tab, setTab] = useState<"document" | "cells">("document");
  const [block, setBlock] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [step, setStep] = useState<"fields" | "tables" | "save">("fields");
  const [tableIndex, setTableIndex] = useState(0);
  const [rowPick, setRowPick] = useState<"firstRow" | "lastRow" | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    listAnnotations()
      .then((result) => {
        if (active) setDocuments(result.documents);
      })
      .catch((cause) => {
        if (active) setError(message(cause));
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);
  const canLeave = () =>
    !dirty ||
    window.confirm("Есть несохранённые изменения. Перейти без сохранения?");
  const refresh = async () => setDocuments((await listAnnotations()).documents);
  const activate = (next: AnnotationDocument) => {
    const pendingField = fieldOrder.find((key) =>
      next.annotation.fields.some(
        (f) => f.field === key && f.state === "pending",
      ),
    );
    const pendingTable = next.annotation.tables.findIndex((t) => !t.reviewed);
    const resumedStep =
      pendingField ? "fields"
      : pendingTable >= 0 ? "tables"
      : "save";
    const source = next.cells.find(
      (c) =>
        c.id ===
        next.annotation.fields.find((f) => f.field === pendingField)?.cell,
    );
    setDoc(next);
    setDirty(false);
    setConfirmed(false);
    setBlock(
      resumedStep === "tables" ?
        next.annotation.tables[pendingTable].block
      : (source?.block ?? next.cells[0]?.block ?? ""),
    );
    setTab(resumedStep === "tables" ? "cells" : "document");
    setBinding(pendingField ?? "supplier");
    setStep(resumedStep);
    setTableIndex(Math.max(0, pendingTable));
    setRowPick(null);
  };
  const open = async (id: string) => {
    if (!canLeave()) return;
    setBusy(true);
    setError("");
    setNotice("");
    setDoc(null);
    setDirty(false);
    try {
      activate(await getAnnotation(id));
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  };
  const upload = async (files: File[]) => {
    if (!files.length || !canLeave()) return;
    if (files.length > 50) {
      setError(
        "За один раз выберите до 50 файлов. Остальные добавьте следующей порцией.",
      );
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    setDoc(null);
    setDirty(false);
    const failures: string[] = [];
    let last: AnnotationDocument | null = null;
    try {
      for (const [index, file] of files.entries()) {
        setProgress(`Читаем ${index + 1} из ${files.length}: ${file.name}`);
        try {
          if (file.size > 25 * 1024 * 1024)
            throw new Error("Файл превышает 25 МБ");
          last = await uploadAnnotation(file);
        } catch (cause) {
          failures.push(`${file.name}: ${message(cause)}`);
        }
      }
      if (last) activate(last);
      await refresh();
      setNotice(
        `Документов доступно: ${files.length - failures.length}. ${last?.duplicate ? "Повторный файл открыт из сохранённых." : "Оригиналы сохранены. Теперь проверьте разметку."}`,
      );
      setError(failures.join("\n"));
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
      setProgress("");
    }
  };
  const update = (
    change: (
      annotation: AnnotationDocument["annotation"],
    ) => AnnotationDocument["annotation"],
  ) => {
    setDoc((current) =>
      current ?
        { ...current, annotation: change(current.annotation) }
      : current,
    );
    setDirty(true);
    setConfirmed(false);
    setNotice("");
  };
  const patchField = (key: string, patch: Partial<MarkedField>) =>
    update((annotation) => ({
      ...annotation,
      fields: annotation.fields.map((field) =>
        field.field === key ? { ...field, ...patch } : field,
      ),
    }));
  const chooseCell = (cell: SourceCell) => {
    if (busy || step !== "fields") return;
    patchField(binding, { cell: cell.id, value: cell.text, state: "pending" });
    setNotice(
      `Ответ выбран. Оставьте нужную часть текста и нажмите «Верно, дальше».`,
    );
  };
  const patchTable = (key: string, patch: Partial<MarkedTable>) =>
    update((annotation) => ({
      ...annotation,
      tables: annotation.tables.map((table) =>
        table.block === key ? { ...table, ...patch } : table,
      ),
    }));
  const goStep = (next: "fields" | "tables" | "save") => {
    setStep(next);
    setRowPick(null);
    setNotice("");
    if (next === "tables" && doc?.annotation.tables.length) {
      setBlock(
        doc.annotation.tables[tableIndex]?.block ??
          doc.annotation.tables[0].block,
      );
      setTab("cells");
    }
    if (next === "save" && doc && !doc.annotation.group.trim()) {
      const supplier = doc.annotation.fields.find(
        (f) => f.field === "supplier" && f.state === "found",
      );
      update((annotation) => ({
        ...annotation,
        group: supplier?.value || doc.filename.replace(/\.[^.]+$/, ""),
      }));
    }
  };
  const nextField = (state: "found" | "missing") => {
    if (!doc) return;
    const field = doc.annotation.fields.find((f) => f.field === binding);
    const source = field && fieldSource(doc.cells, field);
    if (state === "found" && !source) {
      setError(
        "Выберите ответ справа. В поле должна остаться точная часть выбранного текста.",
      );
      return;
    }
    if (
      state === "found" &&
      binding === "documentTotal" &&
      !/^\+?\d[\d\s\u00a0\u202f.,]*$/.test(field?.value.trim() ?? "")
    ) {
      setError(
        "В общем итоге оставьте только число, например «3 700». Уберите слова «Итого» и обозначение валюты.",
      );
      return;
    }
    setError("");
    patchField(
      binding,
      state === "missing" ?
        { state, cell: "", value: "" }
      : { state, cell: source!.id, value: field!.value.trim() },
    );
    const next =
      fieldOrder
        .slice(fieldOrder.indexOf(binding) + 1)
        .find((key) =>
          doc.annotation.fields.some(
            (f) => f.field === key && f.state === "pending",
          ),
        ) ??
      fieldOrder.find(
        (key) =>
          key !== binding &&
          doc.annotation.fields.some(
            (f) => f.field === key && f.state === "pending",
          ),
      );
    if (next) {
      setBinding(next);
      const cell = doc.cells.find(
        (c) =>
          c.id === doc.annotation.fields.find((f) => f.field === next)?.cell,
      );
      if (cell) setBlock(cell.block);
    } else goStep("tables");
  };
  const save = async (status: "draft" | "reviewed") => {
    if (!doc) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await saveAnnotation(doc, status);
      setDoc(saved);
      setDirty(false);
      setConfirmed(false);
      await refresh();
      setNotice(
        status === "reviewed" ?
          "Проверенная разметка сохранена и доступна для экспорта."
        : "Черновик сохранён. Можно продолжить позже.",
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  };
  const download = async () => {
    setBusy(true);
    setError("");
    try {
      await exportAnnotations();
      setNotice(
        "Датасет выгружен. Проверьте количество примеров в train/val/test перед обучением.",
      );
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  };
  const checkedCount =
    doc?.annotation.fields.filter((field) => field.state !== "pending")
      .length ?? 0;
  const checkedTables =
    doc?.annotation.tables.filter((table) => table.reviewed).length ?? 0;
  const canReview =
    !!doc &&
    !doc.parseError &&
    doc.cells.length > 0 &&
    checkedCount === doc.annotation.fields.length &&
    checkedTables === doc.annotation.tables.length &&
    !!doc.annotation.group.trim() &&
    confirmed;
  const reviewedCount = documents.filter(
    (item) => item.status === "reviewed",
  ).length;
  const visible = documents.filter(
    (item) =>
      item.filename.toLocaleLowerCase().includes(filter.toLocaleLowerCase()) &&
      (statusFilter === "all" || item.status === statusFilter),
  );

  return (
    <main className="annotation-app">
      <header className="annotation-header">
        <div>
          <span className="eyebrow">READ DOCUMENT · ОБУЧЕНИЕ</span>
          <h1>Разметка коммерческих предложений</h1>
          <p>Заполните форму слева, сверяя ответы с документом справа.</p>
        </div>
        <div className="annotation-actions">
          <button
            className="outline-cta"
            disabled={busy}
            onClick={() => {
              if (canLeave()) onBack();
            }}>
            К чтению КП
          </button>
          <button
            className="outline-cta"
            disabled={busy || reviewedCount === 0}
            onClick={() => void download()}>
            Экспорт датасета · {reviewedCount}
          </button>
          <button
            className="primary-cta"
            disabled={busy}
            onClick={() => input.current?.click()}>
            Добавить документы
          </button>
          <input
            ref={input}
            hidden
            type="file"
            multiple
            accept={formats}
            onChange={(event) => {
              const files = Array.from(event.target.files ?? []);
              event.target.value = "";
              void upload(files);
            }}
          />
        </div>
      </header>
      <div className="annotation-shell">
        <aside
          className="annotation-library"
          aria-label="Сохранённые документы">
          <h2>
            Документы <span>{documents.length}</span>
          </h2>
          <input
            aria-label="Поиск документов"
            placeholder="Найти по имени…"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <select
            aria-label="Фильтр статуса"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="all">Все документы</option>
            <option value="draft">Черновики</option>
            <option value="reviewed">Проверенные</option>
          </select>
          <div className="annotation-document-list">
            {visible.map((item) => (
              <button
                key={item.id}
                disabled={busy}
                className={doc?.id === item.id ? "selected" : ""}
                onClick={() => void open(item.id)}>
                <strong>{item.filename}</strong>
                <span className={`annotation-badge ${item.status}`}>
                  {item.status === "reviewed" ? "Проверено" : "Черновик"}
                </span>
              </button>
            ))}
          </div>
          {!documents.length && (
            <p className="annotation-hint">
              Добавьте первые КП. Оригиналы и черновики хранятся на сервере.
            </p>
          )}
          <details className="annotation-help">
            <summary>Как разметить документ</summary>
            <ol>
              <li>
                Ответьте на один вопрос слева: подтвердите подсказку или
                выберите другой ответ справа.
              </li>
              <li>
                Если ответа нет в документе, нажмите «В документе не указано».
              </li>
              <li>Следующий вопрос появится автоматически.</li>
              <li>Затем проверьте каждый фрагмент таблиц по очереди.</li>
              <li>На последнем шаге сохраните проверенный результат.</li>
            </ol>
            <p>
              Ошибки OCR сначала исправьте в исходном документе и загрузите его
              заново. Не размечайте выдуманные значения.
            </p>
          </details>
        </aside>
        <section className="annotation-main">
          {error && (
            <div className="annotation-error" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="annotation-notice" role="status">
              {notice}
            </div>
          )}
          {progress && (
            <div className="annotation-notice" role="status">
              <span className="annotation-loading" />
              {progress}
            </div>
          )}
          {!doc && !busy && (
            <div className="annotation-empty">
              <span>01 / ПОДГОТОВКА ДАТАСЕТА</span>
              <h2>Каждый КП — один проверенный документ</h2>
              <p>
                Добавьте файлы или откройте сохранённый черновик. Вы отмечаете
                правильные ответы, приложение формирует JSON для обучения.
              </p>
              <button
                className="primary-cta"
                onClick={() => input.current?.click()}>
                Выбрать КП
              </button>
              <p className="annotation-hint">
                PDF · DOCX · XLSX · XLS · CSV · TSV · TXT · JSON · PNG · JPG ·
                WEBP
                <br />
                До 25 МБ на файл · до 50 файлов за загрузку
              </p>
            </div>
          )}
          {!doc && busy && !progress && (
            <p role="status">Открываем документ…</p>
          )}
          {doc && (
            <>
              <div className="annotation-document-heading">
                <div>
                  <h2>{doc.filename}</h2>
                  <p>
                    {doc.cells.length} ячеек · версия {doc.revision} ·{" "}
                    {dirty ?
                      "Есть несохранённые изменения"
                    : doc.status === "reviewed" ?
                      "Проверенная версия сохранена"
                    : "Черновик сохранён"}
                  </p>
                </div>
                <a
                  className="outline-cta"
                  href={`/api/annotations/${doc.id}/original`}>
                  Скачать оригинал
                </a>
              </div>
              {doc.parseError && (
                <div className="annotation-error">
                  Превью доступно, но ячейки не прочитаны: {doc.parseError}.
                  Сохраните черновик; проверенный экспорт заблокирован.
                </div>
              )}
              {doc.automation?.quarantined && (
                <div className="validation-box" role="status">
                  Карантин: документ исключён из обучения. Не удалось надёжно извлечь или проверить часть данных.
                </div>
              )}
              {doc.automation && !doc.automation.quarantined && doc.status !== "reviewed" && (
                <div className="annotation-hint" role="status">
                  {doc.automation.status === "auto_validated"
                    ? "Авторазметка прошла программные проверки. Проверка человеком ещё не выполнена."
                    : "Авторазметка требует проверки. Причины указаны в предупреждениях документа."}
                  {doc.automation.split === "test" && " Это документ отдельного тестового набора."}
                </div>
              )}
              <nav
                className="annotation-wizard-steps"
                aria-label="Этапы разметки">
                <button
                  disabled={busy}
                  aria-current={step === "fields" ? "step" : undefined}
                  onClick={() => goStep("fields")}>
                  1. Ответы о документе{" "}
                  <small>
                    {checkedCount}/{doc.annotation.fields.length}
                  </small>
                </button>
                <button
                  disabled={busy}
                  aria-current={step === "tables" ? "step" : undefined}
                  onClick={() => goStep("tables")}>
                  2. Проверка таблиц{" "}
                  <small>
                    {checkedTables}/{doc.annotation.tables.length}
                  </small>
                </button>
                <button
                  disabled={busy}
                  aria-current={step === "save" ? "step" : undefined}
                  onClick={() => goStep("save")}>
                  3. Сохранить результат
                </button>
              </nav>
              <div className="annotation-columns">
                <div className="annotation-form-panel">
                  <fieldset disabled={busy} className="annotation-fieldset">
                    {step === "fields" && (
                      <section className="annotation-section">
                        <div className="annotation-section-heading">
                          <h3>{questions[binding]}</h3>
                          <span>
                            {fieldOrder.indexOf(binding) + 1} /{" "}
                            {doc.annotation.fields.length}
                          </span>
                        </div>
                        <p className="annotation-hint">
                          Если ответ ниже правильный — нажмите «Верно, дальше».
                          Если нет — выберите нужный текст справа. Если ответа в
                          документе нет — «В документе не указано».
                        </p>
                        {binding === "documentTotal" && (
                          <p className="annotation-hint">
                            В ответе оставьте только число из документа,
                            например «3 700», без слов «Итого».
                          </p>
                        )}
                        {doc.annotation.fields
                          .filter((field) => field.field === binding)
                          .map((field) => {
                            const source = fieldSource(doc.cells, field);
                            return (
                              <div
                                key={field.field}
                                className="annotation-field active">
                                <div className="annotation-field-heading">
                                  <label htmlFor={`value-${field.field}`}>
                                    {labels[field.field]}
                                  </label>
                                  <span
                                    className={`annotation-state ${field.state}`}>
                                    {field.state === "found" ?
                                      "Указано"
                                    : field.state === "missing" ?
                                      "Не указано"
                                    : "Не проверено"}
                                  </span>
                                </div>
                                <input
                                  id={`value-${field.field}`}
                                  value={field.value}
                                  placeholder="Точная цитата из ячейки"
                                  onFocus={() => setBinding(field.field)}
                                  onChange={(event) => {
                                    const value = event.target.value;
                                    const source = fieldSource(doc.cells, {
                                      ...field,
                                      value,
                                    });
                                    patchField(field.field, {
                                      value,
                                      cell: source?.id ?? "",
                                      state: "pending",
                                    });
                                    setError("");
                                  }}
                                />
                                {source && (
                                  <div className="annotation-answer-proof">
                                    <small>
                                      Этот ответ найден в документе:
                                    </small>
                                    <blockquote>{source.text}</blockquote>
                                  </div>
                                )}
                                {!source && field.value.trim() && (
                                  <p className="annotation-hint" role="status">
                                    Текст не найден однозначно. Выберите нужный
                                    источник справа и проверьте точное
                                    написание.
                                  </p>
                                )}
                                <div className="annotation-small-actions">
                                  <button
                                    type="button"
                                    onClick={() => {
                                      setTab("cells");
                                      if (source) setBlock(source.block);
                                    }}>
                                    Выбрать другой ответ справа →
                                  </button>
                                </div>
                                <div className="annotation-answer-actions">
                                  <button
                                    type="button"
                                    className="primary-cta"
                                    disabled={!source}
                                    onClick={() => nextField("found")}>
                                    Верно, дальше →
                                  </button>
                                  <button
                                    type="button"
                                    className="outline-cta"
                                    onClick={() => nextField("missing")}>
                                    В документе не указано
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        <details className="annotation-answer-overview">
                          <summary>
                            Все ответы · проверено {checkedCount} из{" "}
                            {doc.annotation.fields.length}
                          </summary>
                          {fieldOrder
                            .map((key) =>
                              doc.annotation.fields.find(
                                (f) => f.field === key,
                              ),
                            )
                            .filter((f): f is MarkedField => !!f)
                            .map((field) => (
                              <button
                                type="button"
                                key={field.field}
                                onClick={() => {
                                  setBinding(field.field);
                                  setNotice("");
                                }}>
                                <span>{labels[field.field]}</span>
                                <small>
                                  {field.state === "found" ?
                                    field.value
                                  : field.state === "missing" ?
                                    "Не указано"
                                  : "Нужно проверить"}
                                </small>
                              </button>
                            ))}
                        </details>
                      </section>
                    )}
                    {step === "tables" && (
                      <section className="annotation-section">
                        <h3>Проверим каждую таблицу отдельно</h3>
                        <p className="annotation-hint">
                          Справа показан выбранный фрагмент. Укажите, есть ли в
                          нём товары, затем проверьте первую и последнюю
                          товарные строки и назначение колонок.
                        </p>
                        <div className="annotation-table-picker">
                          {doc.annotation.tables.map((table, index) => (
                            <button
                              key={table.block}
                              type="button"
                              aria-pressed={tableIndex === index}
                              onClick={() => {
                                setTableIndex(index);
                                setBlock(table.block);
                                setTab("cells");
                                setRowPick(null);
                              }}>
                              Фрагмент {index + 1} {table.reviewed ? "✓" : ""}
                            </button>
                          ))}
                        </div>
                        {!doc.annotation.tables.length && (
                          <p>
                            Таблиц не обнаружено. Если в оригинале они есть,
                            сохраните черновик: чтение документа нужно
                            исправить.
                          </p>
                        )}
                        {doc.annotation.tables
                          .filter((_, index) => index === tableIndex)
                          .map((table) => (
                            <TableEditor
                              key={table.block}
                              table={table}
                              cells={doc.cells}
                              onChange={(patch) =>
                                patchTable(table.block, patch)
                              }
                              onPickRow={(target) => {
                                setRowPick(target);
                                setBlock(table.block);
                                setTab("cells");
                              }}
                              onShow={() => {
                                setBlock(table.block);
                                setTab("cells");
                              }}
                            />
                          ))}
                        <button
                          type="button"
                          className="primary-cta"
                          disabled={
                            !!doc.annotation.tables.length &&
                            !doc.annotation.tables[tableIndex]?.reviewed
                          }
                          onClick={() => {
                            const next = doc.annotation.tables.findIndex(
                              (t, i) => i !== tableIndex && !t.reviewed,
                            );
                            if (next >= 0) {
                              setTableIndex(next);
                              setBlock(doc.annotation.tables[next].block);
                              setTab("cells");
                              setRowPick(null);
                            } else goStep("save");
                          }}>
                          {" "}
                          {checkedTables < doc.annotation.tables.length ?
                            "Фрагмент проверен, дальше →"
                          : "К сохранению →"}
                        </button>
                      </section>
                    )}
                    {step === "save" && (
                      <section className="annotation-section">
                        <h3>Проверка завершения</h3>
                        <div className="annotation-completion">
                          <p>
                            Ответы: {checkedCount} из{" "}
                            {doc.annotation.fields.length} проверено
                          </p>
                          <p>
                            Таблицы: {checkedTables} из{" "}
                            {doc.annotation.tables.length} проверено
                          </p>
                        </div>
                        {checkedCount < doc.annotation.fields.length && (
                          <button
                            type="button"
                            className="outline-cta"
                            onClick={() => {
                              setBinding(
                                doc.annotation.fields.find(
                                  (f) => f.state === "pending",
                                )!.field,
                              );
                              goStep("fields");
                            }}>
                            Вернуться к непроверенным ответам
                          </button>
                        )}
                        {checkedTables < doc.annotation.tables.length && (
                          <button
                            type="button"
                            className="outline-cta"
                            onClick={() => {
                              const i = doc.annotation.tables.findIndex(
                                (t) => !t.reviewed,
                              );
                              setTableIndex(i);
                              setBlock(doc.annotation.tables[i].block);
                              setStep("tables");
                              setTab("cells");
                            }}>
                            Проверить оставшиеся таблицы
                          </button>
                        )}
                        <label className="annotation-label">
                          Группа документов / шаблон поставщика
                          <input
                            value={doc.annotation.group}
                            placeholder="Например: альфа-шаблон-2026"
                            onChange={(event) =>
                              update((annotation) => ({
                                ...annotation,
                                group: event.target.value,
                              }))
                            }
                          />
                        </label>
                        <p className="annotation-hint">
                          Для похожих КП и копий в разных форматах задайте
                          одинаковую группу. Она целиком попадёт в train, val
                          или test.
                        </p>
                        <label className="annotation-label">
                          Замечания для себя
                          <textarea
                            rows={3}
                            value={doc.annotation.notes}
                            onChange={(event) =>
                              update((annotation) => ({
                                ...annotation,
                                notes: event.target.value,
                              }))
                            }
                          />
                        </label>
                        {!!doc.warnings.length && (
                          <details>
                            <summary>
                              Предупреждения чтения · {doc.warnings.length}
                            </summary>
                            <ul>
                              {doc.warnings.map((warning, i) => (
                                <li key={i}>{warning}</li>
                              ))}
                            </ul>
                          </details>
                        )}
                      </section>
                    )}
                  </fieldset>
                  <div className="annotation-save-bar">
                    {step === "save" && (
                      <label className="annotation-confirm">
                        <input
                          type="checkbox"
                          checked={confirmed}
                          disabled={busy}
                          onChange={(event) =>
                            setConfirmed(event.target.checked)
                          }
                        />
                        Я сверил реквизиты и таблицы с оригиналом
                      </label>
                    )}
                    <div className="annotation-actions">
                      <button
                        className="outline-cta"
                        disabled={busy}
                        onClick={() => void save("draft")}>
                        Сохранить черновик
                      </button>
                      {step === "save" && (
                        <button
                          className="primary-cta"
                          disabled={busy || !canReview}
                          onClick={() => void save("reviewed")}>
                          {busy ? "Сохраняем…" : "Проверено и сохранить"}
                        </button>
                      )}
                    </div>
                    <small>
                      Черновик можно сохранить на любом шаге и продолжить позже.
                    </small>
                  </div>
                </div>
                <div className="annotation-preview-panel">
                  <div className="annotation-preview-toolbar">
                    <h3>Проверка документа</h3>
                    <div
                      className="annotation-tabs"
                      role="tablist"
                      aria-label="Вид документа">
                      <button
                        role="tab"
                        aria-selected={tab === "document"}
                        onClick={() => setTab("document")}>
                        Документ
                      </button>
                      <button
                        role="tab"
                        aria-selected={tab === "cells"}
                        onClick={() => setTab("cells")}>
                        Ячейки и источники
                      </button>
                    </div>
                  </div>
                  {tab === "document" ?
                    <DocumentPreview
                      key={doc.id}
                      doc={doc}
                      onSource={() => setTab("cells")}
                    />
                  : <SourceBrowser
                      key={doc.id}
                      cells={doc.cells}
                      block={block}
                      onBlock={(value) => {
                        setBlock(value);
                        setRowPick(null);
                        if (step === "tables") {
                          const index = doc.annotation.tables.findIndex(
                            (t) => t.block === value,
                          );
                          if (index >= 0) setTableIndex(index);
                        }
                      }}
                      binding={labels[binding]}
                      readonly={step !== "fields"}
                      rowPrompt={
                        rowPick ?
                          `Нажмите номер ${rowPick === "firstRow" ? "первой" : "последней"} строки с товаром справа.`
                        : undefined
                      }
                      onRow={
                        rowPick ?
                          (row) => {
                            const table = doc.annotation.tables[tableIndex];
                            if (table && table.block === block) {
                              patchTable(table.block, {
                                [rowPick]: row,
                                reviewed: false,
                              });
                              setRowPick(null);
                            }
                          }
                        : undefined
                      }
                      selected={
                        doc.annotation.fields.find(
                          (field) => field.field === binding,
                        )?.cell ?? ""
                      }
                      disabled={busy}
                      onChoose={chooseCell}
                    />
                  }
                </div>
              </div>
            </>
          )}
        </section>
      </div>
    </main>
  );
}

function ColumnSelect({
  label,
  value,
  width,
  names = {},
  onChange,
}: {
  label: string;
  value: number;
  width: number;
  names?: Record<number, string>;
  onChange: (value: number) => void;
}) {
  return (
    <label className="annotation-label">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}>
        <option value={0}>В документе нет</option>
        {Array.from({ length: width }, (_, i) => (
          <option key={i + 1} value={i + 1}>
            {names[i + 1] || `Колонка ${i + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}

function TableEditor({
  table,
  cells,
  onChange,
  onShow,
  onPickRow,
}: {
  table: MarkedTable;
  cells: SourceCell[];
  onChange: (patch: Partial<MarkedTable>) => void;
  onShow: () => void;
  onPickRow: (target: "firstRow" | "lastRow") => void;
}) {
  const blockCells = cells.filter((cell) => cell.block === table.block);
  const width = Math.max(...blockCells.map((cell) => cell.cell));
  const firstRow = Math.min(...blockCells.map((cell) => cell.row));
  const lastRow = Math.max(...blockCells.map((cell) => cell.row));
  const selectedHeaders = new Set(
    [...table.components, ...table.extras].map((c) => c.labelCell),
  );
  const headerCells = cells.filter(
    (cell) =>
      cell.text &&
      (selectedHeaders.has(cell.id) ||
        (cell.block === table.block ?
          cell.row === table.firstRow - 1
        : cell.row <= 3)),
  );
  const names: Record<number, string> = Object.fromEntries(
    Array.from({ length: width }, (_, index) => {
      const col = index + 1;
      const header = blockCells.find(
        (c) => c.cell === col && c.row === table.firstRow - 1,
      );
      const example = blockCells.find(
        (c) => c.cell === col && c.row === table.firstRow,
      );
      return [
        col,
        `${header?.text || `Колонка ${col}`} — ${example?.text.slice(0, 45) || "без значения"}`,
      ];
    }),
  );
  const change = (patch: Partial<MarkedTable>) =>
    onChange({ ...patch, reviewed: false });
  const selectHeader = (id: string) => {
    const cell = cells.find((c) => c.id === id);
    return { labelCell: id, label: cell?.text ?? "" };
  };
  return (
    <div className="annotation-table-editor">
      <p className="annotation-table-location">
        {blockCells[0]?.sheet ?
          `Лист «${blockCells[0].sheet}»`
        : blockCells[0]?.page ?
          `Страница ${blockCells[0].page}`
        : "Фрагмент документа"}{" "}
        · {table.reviewed ? "Проверено" : "Нужно проверить"}
      </p>
      <button type="button" className="annotation-show-block" onClick={onShow}>
        Показать ячейки этого блока →
      </button>
      <label className="annotation-label">
        Есть ли здесь товары или услуги?
        <select
          value={table.isItems ? "items" : "other"}
          onChange={(event) =>
            change({ isItems: event.target.value === "items" })
          }>
          <option value="items">Таблица товаров / услуг</option>
          <option value="other">Реквизиты, итоги или другой текст</option>
        </select>
      </label>
      {table.isItems && (
        <>
          <div className="annotation-grid-two">
            <label className="annotation-label">
              Первая строка товаров
              <input
                type="number"
                min={firstRow}
                max={lastRow}
                value={table.firstRow}
                onChange={(event) =>
                  change({ firstRow: Number(event.target.value) })
                }
              />
              <button
                type="button"
                className="annotation-add"
                onClick={() => onPickRow("firstRow")}>
                Выбрать строку справа →
              </button>
            </label>
            <label className="annotation-label">
              Последняя строка товаров
              <input
                type="number"
                min={firstRow}
                max={lastRow}
                value={table.lastRow}
                onChange={(event) =>
                  change({ lastRow: Number(event.target.value) })
                }
              />
              <button
                type="button"
                className="annotation-add"
                onClick={() => onPickRow("lastRow")}>
                Выбрать строку справа →
              </button>
            </label>
          </div>
          <p className="annotation-hint">
            В блоке строки {firstRow}–{lastRow}, колонок: {width}.
          </p>
          <ColumnSelect
            label="Где название товара?"
            value={table.nameColumn}
            width={width}
            names={names}
            onChange={(value) => change({ nameColumn: value })}
          />
          <div className="annotation-grid-two">
            <ColumnSelect
              label="Где количество?"
              value={table.quantityColumn}
              width={width}
              names={names}
              onChange={(value) => change({ quantityColumn: value })}
            />
            <ColumnSelect
              label="Где единица измерения?"
              value={table.unitColumn}
              width={width}
              names={names}
              onChange={(value) => change({ unitColumn: value })}
            />
          </div>
          <h4>Составляющие стоимости</h4>
          {table.components.map((component, index) => (
            <div className="annotation-cost-editor" key={index}>
              <label className="annotation-label">
                Заголовок стоимости
                <select
                  value={component.labelCell}
                  onChange={(event) =>
                    change({
                      components: table.components.map((c, i) =>
                        i === index ?
                          { ...c, ...selectHeader(event.target.value) }
                        : c,
                      ),
                    })
                  }>
                  <option value="">Выберите название из документа</option>
                  {headerCells.map((cell) => (
                    <option key={cell.id} value={cell.id}>
                      {cell.text.slice(0, 90)}
                    </option>
                  ))}
                </select>
              </label>
              <div className="annotation-grid-two">
                <ColumnSelect
                  label="Цена за единицу"
                  value={component.priceColumn}
                  width={width}
                  names={names}
                  onChange={(value) =>
                    change({
                      components: table.components.map((c, i) =>
                        i === index ? { ...c, priceColumn: value } : c,
                      ),
                    })
                  }
                />
                <ColumnSelect
                  label="Сумма строки"
                  value={component.totalColumn}
                  width={width}
                  names={names}
                  onChange={(value) =>
                    change({
                      components: table.components.map((c, i) =>
                        i === index ? { ...c, totalColumn: value } : c,
                      ),
                    })
                  }
                />
              </div>
              <button
                type="button"
                className="annotation-remove"
                onClick={() =>
                  change({
                    components: table.components.filter((_, i) => i !== index),
                  })
                }>
                Убрать составляющую
              </button>
            </div>
          ))}
          <button
            type="button"
            className="annotation-add"
            disabled={table.components.length >= 10}
            onClick={() =>
              change({
                components: [
                  ...table.components,
                  { label: "", labelCell: "", priceColumn: 0, totalColumn: 0 },
                ],
              })
            }>
            + Стоимость товара, работ или доставки
          </button>
          <h4>Дополнительные колонки</h4>
          {table.extras.map((extra, index) => (
            <div className="annotation-cost-editor" key={index}>
              <label className="annotation-label">
                Заголовок
                <select
                  value={extra.labelCell}
                  onChange={(event) =>
                    change({
                      extras: table.extras.map((c, i) =>
                        i === index ?
                          { ...c, ...selectHeader(event.target.value) }
                        : c,
                      ),
                    })
                  }>
                  <option value="">Выберите заголовок</option>
                  {headerCells.map((cell) => (
                    <option key={cell.id} value={cell.id}>
                      {cell.text.slice(0, 90)}
                    </option>
                  ))}
                </select>
              </label>
              <ColumnSelect
                label="Колонка"
                value={extra.column}
                width={width}
                names={names}
                onChange={(value) =>
                  change({
                    extras: table.extras.map((c, i) =>
                      i === index ? { ...c, column: value } : c,
                    ),
                  })
                }
              />
              <button
                type="button"
                className="annotation-remove"
                onClick={() =>
                  change({ extras: table.extras.filter((_, i) => i !== index) })
                }>
                Убрать колонку
              </button>
            </div>
          ))}
          <button
            type="button"
            className="annotation-add"
            disabled={table.extras.length >= 30}
            onClick={() =>
              change({
                extras: [
                  ...table.extras,
                  { label: "", labelCell: "", column: 1 },
                ],
              })
            }>
            + Артикул, вес или другая колонка
          </button>
        </>
      )}
      <label className="annotation-confirm">
        <input
          type="checkbox"
          checked={table.reviewed}
          onChange={(event) => onChange({ reviewed: event.target.checked })}
        />
        Тип блока{table.isItems ? ", границы и колонки" : ""} проверены
      </label>
    </div>
  );
}

function DocumentPreview({
  doc,
  onSource,
}: {
  doc: AnnotationDocument;
  onSource: () => void;
}) {
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(100);
  const [imageError, setImageError] = useState(false);
  if (doc.preview.kind === "word")
    return (
      <>
        <p className="annotation-preview-caption">
          DOCX: текст, таблицы и встроенные изображения. Разбивка страниц и
          оформление могут отличаться от Word.
        </p>
        <iframe
          className="annotation-word-preview"
          sandbox="allow-same-origin"
          title={`Превью ${doc.filename}`}
          src={`/api/annotations/${doc.id}/preview`}
        />
      </>
    );
  if (doc.preview.kind === "text")
    return (
      <>
        <p className="annotation-preview-caption">
          Исходный текст документа. Для привязки значения откройте «Ячейки и
          источники».
        </p>
        <pre className="annotation-text-preview">{doc.preview.text}</pre>
      </>
    );
  if (doc.preview.kind === "table")
    return (
      <>
        <p className="annotation-preview-caption">
          Содержимое листов и таблиц. Стили, формулы и размеры ячеек оригинала
          здесь не воспроизводятся.
        </p>
        <SourceBrowser
          cells={doc.cells}
          block=""
          readonly
          onChoose={() => {}}
          onBlock={() => {}}
          binding=""
          selected=""
          disabled={false}
        />
      </>
    );
  return (
    <>
      <div className="annotation-page-controls">
        {doc.preview.kind === "pdf" && (
          <>
            <button
              disabled={page <= 1}
              onClick={() => {
                setPage(page - 1);
                setImageError(false);
              }}>
              ←
            </button>
            <label>
              Страница{" "}
              <select
                aria-label="Страница PDF"
                value={page}
                onChange={(event) => {
                  setPage(Number(event.target.value));
                  setImageError(false);
                }}>
                {Array.from({ length: doc.preview.pages ?? 1 }, (_, i) => (
                  <option key={i + 1}>{i + 1}</option>
                ))}
              </select>{" "}
              / {doc.preview.pages}
            </label>
            <button
              disabled={page >= (doc.preview.pages ?? 1)}
              onClick={() => {
                setPage(page + 1);
                setImageError(false);
              }}>
              →
            </button>
          </>
        )}
        <label>
          Масштаб{" "}
          <select
            aria-label="Масштаб превью"
            value={zoom}
            onChange={(event) => setZoom(Number(event.target.value))}>
            {[75, 100, 125, 150, 200].map((value) => (
              <option key={value} value={value}>
                {value}%
              </option>
            ))}
          </select>
        </label>
      </div>
      {imageError ?
        <div className="annotation-error">
          Не удалось загрузить страницу. Повторно откройте документ или скачайте
          оригинал.
        </div>
      : <div className="annotation-page-scroll">
          <img
            key={page}
            style={{ width: `${zoom}%`, maxWidth: "none" }}
            src={`/api/annotations/${doc.id}/pages/${page}`}
            alt={`${doc.filename}, страница ${page}`}
            onError={() => setImageError(true)}
          />
        </div>
      }
      <button className="annotation-source-button" onClick={onSource}>
        Выбрать значение из ячеек →
      </button>
    </>
  );
}

function SourceBrowser({
  cells,
  block,
  onBlock,
  binding,
  selected,
  disabled,
  onChoose,
  readonly = false,
  onRow,
  rowPrompt,
}: {
  cells: SourceCell[];
  block: string;
  onBlock: (block: string) => void;
  binding: string;
  selected: string;
  disabled: boolean;
  onChoose: (cell: SourceCell) => void;
  readonly?: boolean;
  onRow?: (row: number) => void;
  rowPrompt?: string;
}) {
  const [localBlock, setLocalBlock] = useState(cells[0]?.block ?? "");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const blocks = useMemo(
    () => Array.from(new Set(cells.map((cell) => cell.block))),
    [cells],
  );
  const currentBlock = block || (readonly ? localBlock : blocks[0]);
  const blockCells = useMemo(
    () => cells.filter((cell) => cell.block === currentBlock),
    [cells, currentBlock],
  );
  const rows = useMemo(() => {
    const grouped = new Map<number, SourceCell[]>();
    for (const cell of blockCells) {
      if (!grouped.has(cell.row)) grouped.set(cell.row, []);
      grouped.get(cell.row)!.push(cell);
    }
    return [...grouped.entries()].filter(
      ([row, entries]) =>
        !search ||
        String(row) === search ||
        entries.some(
          (cell) =>
            cell.text
              .toLocaleLowerCase()
              .includes(search.toLocaleLowerCase()) || cell.id === search,
        ),
    );
  }, [blockCells, search]);
  const width = Math.max(1, ...blockCells.map((cell) => cell.cell));
  const location = blockCells[0];
  const isTableBlock =
    blockCells.some((cell) => cell.kind === "table") ||
    (blockCells.every((cell) => !cell.kind) &&
      /(^|-)table(?:-|$)|^sheet-|^text-table$|^json-(?!fields$)/.test(
        currentBlock,
      ));
  // Reset display offset without an effect when the block/filter changes.
  const pageOffset = Math.min(
    offset,
    Math.max(0, Math.ceil(rows.length / 100) * 100 - 100),
  );
  return (
    <div className="annotation-source-browser">
      {!readonly && (
        <div className="annotation-binding">
          Выбираем: <strong>{binding}</strong>. Нажмите на текст с правильным
          ответом.
        </div>
      )}
      {rowPrompt && <div className="annotation-binding">{rowPrompt}</div>}
      <div className="annotation-source-controls">
        <label>
          Блок / лист
          <select
            aria-label={readonly ? "Лист превью" : "Блок источника"}
            value={currentBlock ?? ""}
            onChange={(event) => {
              setLocalBlock(event.target.value);
              onBlock(event.target.value);
              setOffset(0);
            }}>
            {blocks.map((name) => {
              const cellsInBlock = cells.filter((cell) => cell.block === name);
              const table =
                cellsInBlock.some((cell) => cell.kind === "table") ||
                (cellsInBlock.every((cell) => !cell.kind) &&
                  /(^|-)table(?:-|$)|^sheet-|^text-table$|^json-(?!fields$)/.test(
                    name,
                  ));
              return (
                <option key={name} value={name}>
                  {table ? "Таблица" : "Текст"} · {name}
                </option>
              );
            })}
          </select>
        </label>
        <input
          aria-label={readonly ? "Поиск в превью" : "Поиск ячеек"}
          placeholder="Текст, id ячейки или номер строки"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setOffset(0);
          }}
        />
      </div>
      <p className="annotation-hint">
        {location?.sheet ? `Лист: ${location.sheet} · ` : ""}
        {location?.page ? `Страница: ${location.page} · ` : ""}
        {isTableBlock ? `Таблица · строк: ${rows.length} · колонок: ${width}` :
          `Текстовый блок · строк: ${rows.length}`}
      </p>
      {!cells.length ?
        <p className="annotation-preview-caption">
          Нет извлечённых ячеек. Проверьте предупреждение чтения.
        </p>
      : isTableBlock ?
        <div className="annotation-source-table-scroll">
          <table className="annotation-source-table">
            <thead>
              <tr>
                <th>Строка</th>
                {Array.from({ length: width }, (_, i) => (
                  <th key={i}>Кол. {i + 1}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows
                .slice(pageOffset, pageOffset + 100)
                .map(([row, entries]) => (
                  <tr key={row}>
                    <th>
                      {onRow ?
                        <button
                          className="annotation-pick-row"
                          disabled={disabled}
                          onClick={() => onRow(row)}
                          aria-label={`Выбрать строку ${row}`}>
                          {row} →
                        </button>
                      : row}
                    </th>
                    {Array.from({ length: width }, (_, i) => {
                      const cell = entries.find((c) => c.cell === i + 1);
                      return (
                        <td
                          key={i}
                          className={cell?.id === selected ? "selected" : ""}>
                          {cell &&
                            (readonly ?
                              <span>{cell.text}</span>
                            : <button
                                disabled={disabled || !cell.text}
                                title={`${cell.id} · ${cell.method}`}
                                onClick={() => onChoose(cell)}>
                                <small>
                                  {cell.id}
                                  {cell.method === "ocr" ? " · OCR" : ""}
                                </small>
                                {cell.text || "—"}
                              </button>)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      : <div className="annotation-source-text" role="list">
          {rows.slice(pageOffset, pageOffset + 100).map(([row, entries]) => (
            <div className="annotation-source-text-row" role="listitem" key={row}>
              <small>Строка {row}</small>
              <div>
                {entries
                  .sort((left, right) => left.cell - right.cell)
                  .map((cell) =>
                    readonly ?
                      <span key={cell.id}>{cell.text}</span>
                    : <button
                        key={cell.id}
                        disabled={disabled || !cell.text}
                        className={cell.id === selected ? "selected" : ""}
                        title={`${cell.id} · ${cell.method}`}
                        onClick={() => onChoose(cell)}>
                        <small>
                          {cell.id}{cell.method === "ocr" ? " · OCR" : ""}
                        </small>
                        {cell.text || "—"}
                      </button>,
                  )}
              </div>
            </div>
          ))}
        </div>
      }
      {rows.length > 100 && (
        <div className="annotation-page-controls">
          <button
            disabled={pageOffset === 0}
            onClick={() => setOffset(pageOffset - 100)}>
            ←
          </button>
          <span>
            Строки {pageOffset + 1}–{Math.min(pageOffset + 100, rows.length)} из{" "}
            {rows.length}
          </span>
          <button
            disabled={pageOffset + 100 >= rows.length}
            onClick={() => setOffset(pageOffset + 100)}>
            →
          </button>
        </div>
      )}
    </div>
  );
}
