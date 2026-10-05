import { useEffect, useMemo, useRef, useState } from "react";
import { exportAnnotations, getAnnotation, listAnnotations, saveAnnotation, uploadAnnotation } from "./lib/annotationApi";
import type { AnnotationDocument, AnnotationSummary, MarkedField, MarkedTable, SourceCell } from "./lib/annotationApi";
import "./AnnotationWorkspace.css";

const labels: Record<string, string> = {
  title: "Название КП", client: "Покупатель", clientContact: "Контакт покупателя", validUntil: "Срок действия",
  supplier: "Поставщик", currency: "Валюта", vat: "НДС", discount: "Скидка", delivery: "Стоимость доставки",
  paymentTerms: "Условия оплаты", deliveryTerms: "Срок поставки", warranty: "Гарантия",
  documentNumber: "Номер документа", documentDate: "Дата документа", documentTotal: "Общий итог",
};
const formats = ".pdf,.docx,.xlsx,.xls,.csv,.tsv,.txt,.json,.png,.jpg,.jpeg,.webp";
const message = (cause: unknown) => cause instanceof Error ? cause.message : "Не удалось выполнить действие";

export default function AnnotationWorkspace({ onBack }: { onBack: () => void }) {
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
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    listAnnotations().then(result => { if (active) setDocuments(result.documents); })
      .catch(cause => { if (active) setError(message(cause)); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);
  const canLeave = () => !dirty || window.confirm("Есть несохранённые изменения. Перейти без сохранения?");
  const refresh = async () => setDocuments((await listAnnotations()).documents);
  const activate = (next: AnnotationDocument) => {
    setDoc(next); setDirty(false); setConfirmed(false); setBlock(next.cells[0]?.block ?? "");
    setTab("document"); setBinding("supplier");
  };
  const open = async (id: string) => {
    if (!canLeave()) return;
    setBusy(true); setError(""); setNotice(""); setDoc(null); setDirty(false);
    try { activate(await getAnnotation(id)); } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  };
  const upload = async (files: File[]) => {
    if (!files.length || !canLeave()) return;
    if (files.length > 50) { setError("За один раз выберите до 50 файлов. Остальные добавьте следующей порцией."); return; }
    setBusy(true); setError(""); setNotice(""); setDoc(null); setDirty(false);
    const failures: string[] = []; let last: AnnotationDocument | null = null;
    try {
      for (const [index, file] of files.entries()) {
        setProgress(`Читаем ${index + 1} из ${files.length}: ${file.name}`);
        try {
          if (file.size > 25 * 1024 * 1024) throw new Error("Файл превышает 25 МБ");
          last = await uploadAnnotation(file);
        } catch (cause) { failures.push(`${file.name}: ${message(cause)}`); }
      }
      if (last) activate(last);
      await refresh();
      setNotice(`Документов доступно: ${files.length - failures.length}. ${last?.duplicate ? "Повторный файл открыт из сохранённых." : "Оригиналы сохранены. Теперь проверьте разметку."}`);
      setError(failures.join("\n"));
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); setProgress(""); }
  };
  const update = (change: (annotation: AnnotationDocument["annotation"]) => AnnotationDocument["annotation"]) => {
    setDoc(current => current ? { ...current, annotation: change(current.annotation) } : current);
    setDirty(true); setConfirmed(false); setNotice("");
  };
  const patchField = (key: string, patch: Partial<MarkedField>) => update(annotation => ({ ...annotation,
    fields: annotation.fields.map(field => field.field === key ? { ...field, ...patch } : field) }));
  const chooseCell = (cell: SourceCell) => {
    if (busy) return;
    patchField(binding, { cell: cell.id, value: cell.text, state: "found" });
    setNotice(`Ячейка ${cell.id} назначена полю «${labels[binding]}». Оставьте в значении только точную цитату.`);
  };
  const patchTable = (key: string, patch: Partial<MarkedTable>) => update(annotation => ({ ...annotation,
    tables: annotation.tables.map(table => table.block === key ? { ...table, ...patch } : table) }));
  const save = async (status: "draft" | "reviewed") => {
    if (!doc) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const saved = await saveAnnotation(doc, status); setDoc(saved); setDirty(false); setConfirmed(false);
      await refresh(); setNotice(status === "reviewed" ? "Проверенная разметка сохранена и доступна для экспорта." : "Черновик сохранён. Можно продолжить позже.");
    } catch (cause) { setError(message(cause)); }
    finally { setBusy(false); }
  };
  const download = async () => {
    setBusy(true); setError("");
    try { await exportAnnotations(); setNotice("Датасет выгружен. Проверьте количество примеров в train/val/test перед обучением."); }
    catch (cause) { setError(message(cause)); } finally { setBusy(false); }
  };
  const checkedCount = doc?.annotation.fields.filter(field => field.state !== "pending").length ?? 0;
  const checkedTables = doc?.annotation.tables.filter(table => table.reviewed).length ?? 0;
  const canReview = !!doc && !doc.parseError && doc.cells.length > 0 && checkedCount === doc.annotation.fields.length
    && checkedTables === doc.annotation.tables.length && !!doc.annotation.group.trim() && confirmed;
  const reviewedCount = documents.filter(item => item.status === "reviewed").length;
  const visible = documents.filter(item => item.filename.toLocaleLowerCase().includes(filter.toLocaleLowerCase())
    && (statusFilter === "all" || item.status === statusFilter));

  return <main className="annotation-app">
    <header className="annotation-header">
      <div><span className="eyebrow">READ DOCUMENT · ОБУЧЕНИЕ</span><h1>Разметка коммерческих предложений</h1>
        <p>Заполните форму слева, сверяя ответы с документом справа.</p></div>
      <div className="annotation-actions">
        <button className="outline-cta" disabled={busy} onClick={() => { if (canLeave()) onBack(); }}>К чтению КП</button>
        <button className="outline-cta" disabled={busy || reviewedCount === 0} onClick={() => void download()}>Экспорт датасета · {reviewedCount}</button>
        <button className="primary-cta" disabled={busy} onClick={() => input.current?.click()}>Добавить документы</button>
        <input ref={input} hidden type="file" multiple accept={formats} onChange={event => {
          const files = Array.from(event.target.files ?? []); event.target.value = ""; void upload(files);
        }} />
      </div>
    </header>
    <div className="annotation-shell">
      <aside className="annotation-library" aria-label="Сохранённые документы">
        <h2>Документы <span>{documents.length}</span></h2>
        <input aria-label="Поиск документов" placeholder="Найти по имени…" value={filter} onChange={event => setFilter(event.target.value)} />
        <select aria-label="Фильтр статуса" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>
          <option value="all">Все документы</option><option value="draft">Черновики</option><option value="reviewed">Проверенные</option>
        </select>
        <div className="annotation-document-list">{visible.map(item => <button key={item.id} disabled={busy}
          className={doc?.id === item.id ? "selected" : ""} onClick={() => void open(item.id)}>
          <strong>{item.filename}</strong><span className={`annotation-badge ${item.status}`}>{item.status === "reviewed" ? "Проверено" : "Черновик"}</span>
        </button>)}</div>
        {!documents.length && <p className="annotation-hint">Добавьте первые КП. Оригиналы и черновики хранятся на сервере.</p>}
        <details className="annotation-help"><summary>Как разметить документ</summary>
          <ol><li>Выберите поле слева и нажмите «Выбрать ячейку».</li><li>Справа нажмите ячейку с ответом.</li>
            <li>Оставьте точную цитату. Если данных нет — «Не указано».</li><li>Укажите колонки и границы товарных строк.</li>
            <li>Введите группу шаблона и сохраните проверенный документ.</li></ol>
          <p>Ошибки OCR сначала исправьте в исходном документе и загрузите его заново. Не размечайте выдуманные значения.</p>
        </details>
      </aside>
      <section className="annotation-main">
        {error && <div className="annotation-error" role="alert">{error}</div>}
        {notice && <div className="annotation-notice" role="status">{notice}</div>}
        {progress && <div className="annotation-notice" role="status"><span className="annotation-loading" />{progress}</div>}
        {!doc && !busy && <div className="annotation-empty"><span>01 / ПОДГОТОВКА ДАТАСЕТА</span>
          <h2>Каждый КП — один проверенный документ</h2><p>Добавьте файлы или откройте сохранённый черновик. Вы отмечаете правильные ответы, приложение формирует JSON для обучения.</p>
          <button className="primary-cta" onClick={() => input.current?.click()}>Выбрать КП</button>
          <p className="annotation-hint">PDF · DOCX · XLSX · XLS · CSV · TSV · TXT · JSON · PNG · JPG · WEBP<br />До 25 МБ на файл · до 50 файлов за загрузку</p></div>}
        {!doc && busy && !progress && <p role="status">Открываем документ…</p>}
        {doc && <>
          <div className="annotation-document-heading"><div><h2>{doc.filename}</h2>
            <p>{doc.cells.length} ячеек · версия {doc.revision} · {dirty ? "Есть несохранённые изменения" : doc.status === "reviewed" ? "Проверенная версия сохранена" : "Черновик сохранён"}</p></div>
            <a className="outline-cta" href={`/api/annotations/${doc.id}/original`}>Скачать оригинал</a></div>
          {doc.parseError && <div className="annotation-error">Превью доступно, но ячейки не прочитаны: {doc.parseError}. Сохраните черновик; проверенный экспорт заблокирован.</div>}
          <div className="annotation-columns">
            <div className="annotation-form-panel">
              <fieldset disabled={busy} className="annotation-fieldset">
                <section className="annotation-section"><div className="annotation-section-heading"><h3>1. Реквизиты</h3><span>{checkedCount} / {doc.annotation.fields.length}</span></div>
                  <p className="annotation-hint">Подсказки получены локальными правилами. Каждое поле нужно проверить.</p>
                  {doc.annotation.fields.map(field => <div key={field.field} className={`annotation-field ${binding === field.field ? "active" : ""}`}>
                    <div className="annotation-field-heading"><label htmlFor={`value-${field.field}`}>{labels[field.field]}</label>
                      <span className={`annotation-state ${field.state}`}>{field.state === "found" ? "Указано" : field.state === "missing" ? "Не указано" : "Не проверено"}</span></div>
                    <input id={`value-${field.field}`} value={field.value} placeholder="Точная цитата из ячейки"
                      onFocus={() => setBinding(field.field)} onChange={event => patchField(field.field, { value: event.target.value, state: "pending" })} />
                    {field.cell && <small className="annotation-source-link">Источник: {field.cell} · {doc.cells.find(cell => cell.id === field.cell)?.text}</small>}
                    <div className="annotation-small-actions">
                      <button type="button" onClick={() => { setBinding(field.field); setTab("cells"); const cell = doc.cells.find(c => c.id === field.cell); if (cell) setBlock(cell.block); }}>Выбрать ячейку</button>
                      <button type="button" disabled={!field.cell || !field.value.trim()} onClick={() => patchField(field.field, { state: "found" })}>Подтвердить</button>
                      <button type="button" onClick={() => patchField(field.field, { state: "missing", cell: "", value: "" })}>Не указано</button>
                    </div>
                  </div>)}
                </section>
                <section className="annotation-section"><div className="annotation-section-heading"><h3>2. Таблицы и колонки</h3><span>{checkedTables} / {doc.annotation.tables.length}</span></div>
                  <p className="annotation-hint">Исключите из диапазона товаров заголовки, итоги и реквизиты. Каждый блок классифицируйте явно.</p>
                  {!doc.annotation.tables.length && <p>Табличных блоков не обнаружено.</p>}
                  {doc.annotation.tables.map(table => <TableEditor key={table.block} table={table} cells={doc.cells}
                    onChange={patch => patchTable(table.block, patch)} onShow={() => { setBlock(table.block); setTab("cells"); }} />)}
                </section>
                <section className="annotation-section"><h3>3. Группа и замечания</h3>
                  <label className="annotation-label">Группа документов / шаблон поставщика<input value={doc.annotation.group} placeholder="Например: альфа-шаблон-2026"
                    onChange={event => update(annotation => ({ ...annotation, group: event.target.value }))} /></label>
                  <p className="annotation-hint">Для похожих КП и копий в разных форматах задайте одинаковую группу. Она целиком попадёт в train, val или test.</p>
                  <label className="annotation-label">Замечания для себя<textarea rows={3} value={doc.annotation.notes}
                    onChange={event => update(annotation => ({ ...annotation, notes: event.target.value }))} /></label>
                  {!!doc.warnings.length && <details><summary>Предупреждения чтения · {doc.warnings.length}</summary><ul>{doc.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details>}
                </section>
              </fieldset>
              <div className="annotation-save-bar">
                <label className="annotation-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={event => setConfirmed(event.target.checked)} />Я сверил реквизиты и таблицы с оригиналом</label>
                <div className="annotation-actions"><button className="outline-cta" disabled={busy} onClick={() => void save("draft")}>Сохранить черновик</button>
                  <button className="primary-cta" disabled={busy || !canReview} onClick={() => void save("reviewed")}>{busy ? "Сохраняем…" : "Проверено и сохранить"}</button></div>
                <small>Для проверенной версии отметьте все поля и блоки, укажите группу и подтвердите сверку. Правки сохраняются только по кнопке.</small>
              </div>
            </div>
            <div className="annotation-preview-panel">
              <div className="annotation-preview-toolbar"><h3>Проверка документа</h3><div className="annotation-tabs" role="tablist" aria-label="Вид документа">
                <button role="tab" aria-selected={tab === "document"} onClick={() => setTab("document")}>Документ</button>
                <button role="tab" aria-selected={tab === "cells"} onClick={() => setTab("cells")}>Ячейки и источники</button></div></div>
              {tab === "document" ? <DocumentPreview key={doc.id} doc={doc} onSource={() => setTab("cells")} /> :
                <SourceBrowser key={doc.id} cells={doc.cells} block={block} onBlock={setBlock} binding={labels[binding]}
                  selected={doc.annotation.fields.find(field => field.field === binding)?.cell ?? ""} disabled={busy} onChoose={chooseCell} />}
            </div>
          </div>
        </>}
      </section>
    </div>
  </main>;
}

function ColumnSelect({ label, value, width, onChange }: { label: string; value: number; width: number; onChange: (value: number) => void }) {
  return <label className="annotation-label">{label}<select value={value} onChange={event => onChange(Number(event.target.value))}>
    <option value={0}>Нет колонки</option>{Array.from({ length: width }, (_, i) => <option key={i + 1} value={i + 1}>Колонка {i + 1}</option>)}
  </select></label>;
}

function TableEditor({ table, cells, onChange, onShow }: { table: MarkedTable; cells: SourceCell[]; onChange: (patch: Partial<MarkedTable>) => void; onShow: () => void }) {
  const blockCells = cells.filter(cell => cell.block === table.block);
  const width = Math.max(...blockCells.map(cell => cell.cell));
  const firstRow = Math.min(...blockCells.map(cell => cell.row));
  const lastRow = Math.max(...blockCells.map(cell => cell.row));
  const headerCells = cells.filter(cell => cell.text && (cell.block === table.block ? cell.row < table.firstRow : cell.row <= 3));
  const change = (patch: Partial<MarkedTable>) => onChange({ ...patch, reviewed: false });
  const selectHeader = (id: string) => { const cell = cells.find(c => c.id === id); return { labelCell: id, label: cell?.text ?? "" }; };
  return <details className="annotation-table-editor" open={table.isItems}>
    <summary>{table.block} <span>{table.reviewed ? "✓ Проверено" : "Не проверено"}</span></summary>
    <button type="button" className="annotation-show-block" onClick={onShow}>Показать ячейки этого блока →</button>
    <label className="annotation-label">Что находится в блоке?<select value={table.isItems ? "items" : "other"}
      onChange={event => change({ isItems: event.target.value === "items" })}><option value="items">Таблица товаров / услуг</option><option value="other">Реквизиты, итоги или другой текст</option></select></label>
    {table.isItems && <>
      <div className="annotation-grid-two">
        <label className="annotation-label">Первая строка товаров<input type="number" min={firstRow} max={lastRow} value={table.firstRow}
          onChange={event => change({ firstRow: Number(event.target.value) })} /></label>
        <label className="annotation-label">Последняя строка товаров<input type="number" min={firstRow} max={lastRow} value={table.lastRow}
          onChange={event => change({ lastRow: Number(event.target.value) })} /></label>
      </div>
      <p className="annotation-hint">В блоке строки {firstRow}–{lastRow}, колонок: {width}.</p>
      <ColumnSelect label="Наименование" value={table.nameColumn} width={width} onChange={value => change({ nameColumn: value })} />
      <div className="annotation-grid-two"><ColumnSelect label="Количество" value={table.quantityColumn} width={width} onChange={value => change({ quantityColumn: value })} />
        <ColumnSelect label="Единица измерения" value={table.unitColumn} width={width} onChange={value => change({ unitColumn: value })} /></div>
      <h4>Составляющие стоимости</h4>
      {table.components.map((component, index) => <div className="annotation-cost-editor" key={index}>
        <label className="annotation-label">Заголовок стоимости<select value={component.labelCell} onChange={event => change({ components: table.components.map((c, i) => i === index ? { ...c, ...selectHeader(event.target.value) } : c) })}>
          <option value="">Выберите заголовок из источника</option>{headerCells.map(cell => <option key={cell.id} value={cell.id}>{cell.id} · {cell.text.slice(0, 90)}</option>)}
        </select></label>
        <div className="annotation-grid-two"><ColumnSelect label="Цена за единицу" value={component.priceColumn} width={width}
          onChange={value => change({ components: table.components.map((c, i) => i === index ? { ...c, priceColumn: value } : c) })} />
          <ColumnSelect label="Сумма строки" value={component.totalColumn} width={width}
            onChange={value => change({ components: table.components.map((c, i) => i === index ? { ...c, totalColumn: value } : c) })} /></div>
        <button type="button" className="annotation-remove" onClick={() => change({ components: table.components.filter((_, i) => i !== index) })}>Убрать составляющую</button>
      </div>)}
      <button type="button" className="annotation-add" disabled={table.components.length >= 10} onClick={() => change({ components: [...table.components, { label: "", labelCell: "", priceColumn: 0, totalColumn: 0 }] })}>+ Стоимость товара, работ или доставки</button>
      <h4>Дополнительные колонки</h4>
      {table.extras.map((extra, index) => <div className="annotation-cost-editor" key={index}>
        <label className="annotation-label">Заголовок<select value={extra.labelCell} onChange={event => change({ extras: table.extras.map((c, i) => i === index ? { ...c, ...selectHeader(event.target.value) } : c) })}>
          <option value="">Выберите заголовок</option>{headerCells.map(cell => <option key={cell.id} value={cell.id}>{cell.id} · {cell.text.slice(0, 90)}</option>)}
        </select></label><ColumnSelect label="Колонка" value={extra.column} width={width} onChange={value => change({ extras: table.extras.map((c, i) => i === index ? { ...c, column: value } : c) })} />
        <button type="button" className="annotation-remove" onClick={() => change({ extras: table.extras.filter((_, i) => i !== index) })}>Убрать колонку</button>
      </div>)}
      <button type="button" className="annotation-add" disabled={table.extras.length >= 30} onClick={() => change({ extras: [...table.extras, { label: "", labelCell: "", column: 1 }] })}>+ Артикул, вес или другая колонка</button>
    </>}
    <label className="annotation-confirm"><input type="checkbox" checked={table.reviewed} onChange={event => onChange({ reviewed: event.target.checked })} />Тип блока{table.isItems ? ", границы и колонки" : ""} проверены</label>
  </details>;
}

function DocumentPreview({ doc, onSource }: { doc: AnnotationDocument; onSource: () => void }) {
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(100);
  const [imageError, setImageError] = useState(false);
  if (doc.preview.kind === "word") return <><p className="annotation-preview-caption">DOCX: текст, таблицы и встроенные изображения. Разбивка страниц и оформление могут отличаться от Word.</p>
    <iframe className="annotation-word-preview" sandbox="allow-same-origin" title={`Превью ${doc.filename}`} src={`/api/annotations/${doc.id}/preview`} /></>;
  if (doc.preview.kind === "text") return <><p className="annotation-preview-caption">Исходный текст документа. Для привязки значения откройте «Ячейки и источники».</p><pre className="annotation-text-preview">{doc.preview.text}</pre></>;
  if (doc.preview.kind === "table") return <><p className="annotation-preview-caption">Содержимое листов и таблиц. Стили, формулы и размеры ячеек оригинала здесь не воспроизводятся.</p>
    <SourceBrowser cells={doc.cells} block="" readonly onChoose={() => {}} onBlock={() => {}} binding="" selected="" disabled={false} /></>;
  return <>
    <div className="annotation-page-controls">
      {doc.preview.kind === "pdf" && <><button disabled={page <= 1} onClick={() => { setPage(page - 1); setImageError(false); }}>←</button>
        <label>Страница <select aria-label="Страница PDF" value={page} onChange={event => { setPage(Number(event.target.value)); setImageError(false); }}>
          {Array.from({ length: doc.preview.pages ?? 1 }, (_, i) => <option key={i + 1}>{i + 1}</option>)}
        </select> / {doc.preview.pages}</label><button disabled={page >= (doc.preview.pages ?? 1)} onClick={() => { setPage(page + 1); setImageError(false); }}>→</button></>}
      <label>Масштаб <select aria-label="Масштаб превью" value={zoom} onChange={event => setZoom(Number(event.target.value))}>
        {[75, 100, 125, 150, 200].map(value => <option key={value} value={value}>{value}%</option>)}</select></label>
    </div>
    {imageError ? <div className="annotation-error">Не удалось загрузить страницу. Повторно откройте документ или скачайте оригинал.</div> :
      <div className="annotation-page-scroll"><img key={page} style={{ width: `${zoom}%`, maxWidth: "none" }}
        src={`/api/annotations/${doc.id}/pages/${page}`} alt={`${doc.filename}, страница ${page}`} onError={() => setImageError(true)} /></div>}
    <button className="annotation-source-button" onClick={onSource}>Выбрать значение из ячеек →</button>
  </>;
}

function SourceBrowser({ cells, block, onBlock, binding, selected, disabled, onChoose, readonly = false }: {
  cells: SourceCell[]; block: string; onBlock: (block: string) => void; binding: string; selected: string; disabled: boolean;
  onChoose: (cell: SourceCell) => void; readonly?: boolean;
}) {
  const [localBlock, setLocalBlock] = useState(cells[0]?.block ?? "");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const blocks = useMemo(() => Array.from(new Set(cells.map(cell => cell.block))), [cells]);
  const currentBlock = readonly ? localBlock : block || blocks[0];
  const blockCells = useMemo(() => cells.filter(cell => cell.block === currentBlock), [cells, currentBlock]);
  const rows = useMemo(() => {
    const grouped = new Map<number, SourceCell[]>();
    for (const cell of blockCells) { if (!grouped.has(cell.row)) grouped.set(cell.row, []); grouped.get(cell.row)!.push(cell); }
    return [...grouped.entries()].filter(([row, entries]) => !search || String(row) === search || entries.some(cell => cell.text.toLocaleLowerCase().includes(search.toLocaleLowerCase()) || cell.id === search));
  }, [blockCells, search]);
  const width = Math.max(1, ...blockCells.map(cell => cell.cell));
  const location = blockCells[0];
  // Reset display offset without an effect when the block/filter changes.
  const pageOffset = Math.min(offset, Math.max(0, Math.ceil(rows.length / 100) * 100 - 100));
  return <div className="annotation-source-browser">
    {!readonly && <div className="annotation-binding">Нажмите ячейку для поля <strong>{binding}</strong>. Затем слева уточните точную цитату.</div>}
    <div className="annotation-source-controls"><label>Блок / лист<select aria-label={readonly ? "Лист превью" : "Блок источника"} value={currentBlock ?? ""}
      onChange={event => { if (readonly) setLocalBlock(event.target.value); else onBlock(event.target.value); setOffset(0); }}>
      {blocks.map(name => <option key={name}>{name}</option>)}</select></label>
      <input aria-label={readonly ? "Поиск в превью" : "Поиск ячеек"} placeholder="Текст, id ячейки или номер строки" value={search} onChange={event => { setSearch(event.target.value); setOffset(0); }} /></div>
    <p className="annotation-hint">{location?.sheet ? `Лист: ${location.sheet} · ` : ""}{location?.page ? `Страница: ${location.page} · ` : ""}Строк: {rows.length} · колонок: {width}</p>
    {!cells.length ? <p className="annotation-preview-caption">Нет извлечённых ячеек. Проверьте предупреждение чтения.</p> :
      <div className="annotation-source-table-scroll"><table className="annotation-source-table"><thead><tr><th>Строка</th>{Array.from({ length: width }, (_, i) => <th key={i}>Кол. {i + 1}</th>)}</tr></thead>
        <tbody>{rows.slice(pageOffset, pageOffset + 100).map(([row, entries]) => <tr key={row}><th>{row}</th>{Array.from({ length: width }, (_, i) => {
          const cell = entries.find(c => c.cell === i + 1);
          return <td key={i} className={cell?.id === selected ? "selected" : ""}>{cell && (readonly ? <span>{cell.text}</span> :
            <button disabled={disabled || !cell.text} title={`${cell.id} · ${cell.method}`} onClick={() => onChoose(cell)}><small>{cell.id}{cell.method === "ocr" ? " · OCR" : ""}</small>{cell.text || "—"}</button>)}</td>;
        })}</tr>)}</tbody></table></div>}
    {rows.length > 100 && <div className="annotation-page-controls"><button disabled={pageOffset === 0} onClick={() => setOffset(pageOffset - 100)}>←</button>
      <span>Строки {pageOffset + 1}–{Math.min(pageOffset + 100, rows.length)} из {rows.length}</span>
      <button disabled={pageOffset + 100 >= rows.length} onClick={() => setOffset(pageOffset + 100)}>→</button></div>}
  </div>;
}
