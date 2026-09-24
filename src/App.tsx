import { useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent, DragEvent } from 'react'
import { parseDocument } from './lib/documentParser'
import { canUseExtractionApi, parseWithExtractionApi, submitProposal } from './lib/extractionApi'
import type { CommercialProposal, ExtractionMetadata, FieldEvidence, ParseResult } from './lib/documentParser'
import './App.css'

type Upload = { file: File; proposal: Partial<CommercialProposal>; metadata: ExtractionMetadata; error?: string }
const blank: CommercialProposal = {
  title: 'Коммерческое предложение', client: '', clientContact: '', validUntil: '', supplier: '',
  currency: '', vat: '', discount: '', delivery: '', paymentTerms: '', deliveryTerms: '', warranty: '',
  documentNumber: '', documentDate: '', documentTotal: 0, notes: '', items: [],
}
const textFields: Array<[keyof CommercialProposal, string]> = [
  ['title', 'Название'], ['client', 'Клиент'], ['clientContact', 'Контакт'], ['validUntil', 'Срок действия'],
  ['supplier', 'Поставщик'], ['documentNumber', 'Номер документа'], ['documentDate', 'Дата документа'],
  ['currency', 'Валюта'], ['vat', 'НДС'], ['discount', 'Скидка'], ['delivery', 'Доставка'],
  ['paymentTerms', 'Условия оплаты'], ['deliveryTerms', 'Сроки поставки'], ['warranty', 'Гарантия'],
]
const money = (value: number, currency: string) => {
  const known = /^[A-Z]{3}$/.test(currency) ? currency : 'RUB'
  if (!currency.trim()) return `${value.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} · валюта не указана`
  try { return new Intl.NumberFormat('ru-RU', { style: 'currency', currency: known, maximumFractionDigits: 2 }).format(value) }
  catch { return `${value.toLocaleString('ru-RU')} ${currency}` }
}
const asEvidence = (value: FieldEvidence | FieldEvidence[] | undefined): FieldEvidence[] =>
  !value ? [] : Array.isArray(value) ? value : [value]

function App() {
  const [uploads, setUploads] = useState<Upload[]>([])
  const [proposal, setProposal] = useState<CommercialProposal>(blank)
  const [error, setError] = useState('')
  const [reading, setReading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [editing, setEditing] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [saved, setSaved] = useState<{ id: string; createdAt: string } | null>(null)
  const [previewFile, setPreviewFile] = useState<string | null>(null)
  const [resolvedConflicts, setResolvedConflicts] = useState<string[]>([])
  const inputRef = useRef<HTMLInputElement>(null)
  const total = useMemo(() => proposal.items.reduce((sum, item) => sum + item.quantity * item.unitPrice, 0), [proposal.items])

  const processFiles = async (files: File[]) => {
    const acceptable = files.map((file) => ({ file, error: file.size > 25 * 1024 * 1024 ? 'Файл превышает 25 МБ' : '' }))
    if (!acceptable.length) return
    setError('')
    setReading(true)
    setSaved(null)
    try {
      const results = await Promise.all(acceptable.map(async ({ file, error: sizeError }): Promise<Upload> => {
        if (sizeError) return { file, proposal: {}, metadata: { sourceName: file.name, parser: 'Проверка файла', status: 'error', confidence: 0, warnings: [sizeError] }, error: sizeError }
        try {
          let result: ParseResult
          if (canUseExtractionApi(file)) {
            try { result = await parseWithExtractionApi(file) }
            catch (cause) {
              if (/\.(pdf|docx)$/i.test(file.name)) result = await parseDocument(file)
              else throw cause
            }
          } else result = await parseDocument(file)
          return { file, proposal: result.proposal, metadata: result.metadata,
            error: result.metadata.status === 'error' || result.metadata.status === 'unsupported' || result.metadata.status === 'empty' ? result.metadata.warnings[0] : undefined }
        } catch (cause) {
          return { file, proposal: {}, metadata: { sourceName: file.name, parser: 'AI OCR', status: 'error', confidence: 0, warnings: [cause instanceof Error ? cause.message : 'Не удалось обработать файл'] }, error: cause instanceof Error ? cause.message : 'Не удалось обработать файл' }
        }
      }))
      setUploads(results)
      const successful = results.filter((upload) => !upload.error)
      const merged = successful.reduce<CommercialProposal>((current, { proposal: next }) => {
        const update = { ...current }
        for (const [key] of textFields) {
          const value = next[key]
          if (typeof value === 'string' && value.trim() && (!update[key] || key === 'title' && update[key] === blank.title)) update[key] = value as never
        }
        if (typeof next.documentTotal === 'number' && next.documentTotal > 0 && !update.documentTotal) update.documentTotal = next.documentTotal
        update.notes = [current.notes, next.notes].filter((part): part is string => typeof part === 'string' && !!part.trim()).join('\n\n')
        update.items = [...current.items, ...(next.items ?? [])]
        return update
      }, { ...blank, items: [] })
      setProposal(merged)
      setEditing(false)
      setConfirmed(false)
      setResolvedConflicts([])
      setPreviewFile(null)
      if (successful.length === 0) setError('Не удалось распознать ни одного файла. Проверьте формат и доступность сервера распознавания.')
    } finally {
      setReading(false)
    }
  }

  const onInput = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) void processFiles(Array.from(event.target.files))
    event.target.value = ''
  }
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    void processFiles(Array.from(event.dataTransfer.files))
  }
  const reset = () => {
    setUploads([]); setProposal(blank); setError(''); setEditing(false); setConfirmed(false); setSaved(null)
    if (inputRef.current) inputRef.current.value = ''
  }

  const conflicts = textFields.flatMap(([key, label]) => {
    const values = uploads.flatMap((upload) => {
      const value = upload.proposal[key]
      return !upload.error && typeof value === 'string' && value.trim() ? [{ value, file: upload.file.name }] : []
    })
    return new Set(values.map(({ value }) => value.trim().toLocaleLowerCase())).size > 1 ? [{ key, label, values }] : []
  })
  const duplicateItems = proposal.items.filter((item, index) => proposal.items.findIndex((candidate) => candidate.name.trim().toLowerCase() === item.name.trim().toLowerCase()) !== index)
  const validation = [
    ...(!proposal.client.trim() ? ['Укажите клиента'] : []),
    ...(!proposal.validUntil.trim() ? ['Уточните срок действия'] : []),
    ...(!proposal.items.length ? ['Добавьте хотя бы одну позицию'] : []),
    ...proposal.items.flatMap((item, index) => [
      ...(!item.name.trim() ? [`Позиция ${index + 1}: не указано название`] : []),
      ...(!(item.quantity > 0) ? [`${item.name || `Позиция ${index + 1}`}: количество должно быть больше нуля`] : []),
      ...(!(item.unitPrice >= 0) ? [`${item.name || `Позиция ${index + 1}`}: цена не может быть отрицательной`] : []),
    ]),
    ...(conflicts.some(({ key }) => !resolvedConflicts.includes(key)) ? [`Разрешите конфликты в полях: ${conflicts.filter(({ key }) => !resolvedConflicts.includes(key)).map(({ label }) => label).join(', ')}`] : []),
  ]
  const canConfirm = validation.length === 0

  const chooseConflict = (key: keyof CommercialProposal, value: string) => {
    setProposal((current) => ({ ...current, [key]: value }))
    setResolvedConflicts((current) => current.includes(key) ? current : [...current, key])
    setConfirmed(false); setSaved(null)
  }
  const confirm = () => { if (canConfirm) { setEditing(false); setConfirmed(true) } }
  const send = async () => {
    setSubmitting(true); setError('')
    try {
      const result = await submitProposal({ proposal, sources: uploads.map(({ file, metadata }) => ({ name: file.name, parser: metadata.parser, confidence: metadata.confidence })) })
      setSaved({ id: result.id, createdAt: result.createdAt })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Не удалось сохранить предложение')
    } finally { setSubmitting(false) }
  }
  const download = (kind: 'json' | 'csv') => {
    const content = kind === 'json'
      ? JSON.stringify({ proposal, sources: uploads.map(({ file, metadata }) => ({ name: file.name, metadata })), total }, null, 2)
      : [['Наименование', 'Количество', 'Единица', 'Цена', 'Сумма'], ...proposal.items.map((item) => [item.name, item.quantity, item.unit, item.unitPrice, item.quantity * item.unitPrice])]
        .map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(';')).join('\r\n')
    const blob = new Blob([kind === 'csv' ? `\uFEFF${content}` : content], { type: kind === 'csv' ? 'text/csv;charset=utf-8' : 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `proposal.${kind}`; anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <main className="reader">
      <header className="reader-header"><div><span className="eyebrow">READ DOCUMENT</span><h1>Чтение коммерческого предложения</h1><p>Загрузите документы — проверьте извлеченные данные и сохраните их в системе.</p></div>{uploads.length > 0 && <button className="outline-cta" onClick={reset}>Новое чтение</button>}</header>
      <section className="reader-content">
        {error && <div className="alert" role="alert">{error}</div>}
        {uploads.length === 0 && !reading && <div className={`dropzone ${dragging ? 'dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={onDrop} onClick={() => inputRef.current?.click()}>
          <input ref={inputRef} type="file" multiple accept=".docx,.xlsx,.xls,.csv,.tsv,.txt,.json,.pdf,.png,.jpg,.jpeg,.webp" onChange={onInput} />
          <div className="upload-icon">↑</div><strong>Перетащите документы сюда</strong><p>или <u>выберите файлы на компьютере</u></p><small>PDF, DOCX, таблицы, текст и изображения · до 25 МБ на файл</small>
        </div>}
        {reading && <div className="analysis"><div className="spinner" /><h2>Читаем документы</h2><p>Извлекаем поля и позиции. Распознавание сканов может занять несколько минут.</p></div>}
        {!reading && uploads.length > 0 && <>
          <div className="results-heading"><div><h2>Результаты распознавания</h2><p>{confirmed ? 'Данные проверены и готовы к сохранению.' : 'Проверьте источники, конфликты и поля перед сохранением.'}</p></div><div className="heading-actions">{confirmed && <span className="confirmed-badge">✓ Проверено</span>}<span className="file-count">{uploads.length} {uploads.length === 1 ? 'файл' : 'файлов'}</span></div></div>
          <div className="uploaded-results">{uploads.map(({ file, metadata, error: fileError }) => <div className="result-row" key={`${file.name}-${file.lastModified}`}>
            <button className="source-link" onClick={() => setPreviewFile(previewFile === file.name ? null : file.name)}>{file.name}</button>
            <span className={`status ${fileError ? 'warning' : 'done'}`}>{fileError ? fileError : `✓ ${metadata.parser} · ${Math.round(metadata.confidence * 100)}%${metadata.ocrPages ? ` · OCR страниц: ${metadata.ocrPages}` : ''}`}</span>
          </div>)}</div>
          {previewFile && <SourcePreview upload={uploads.find(({ file }) => file.name === previewFile)!} />}
          {conflicts.length > 0 && <section className="conflict-panel"><h3>Найдены разные значения</h3><p>Выберите значение из источника или исправьте его в редакторе.</p>{conflicts.map(({ key, label, values }) => <label className="conflict-row" key={key}>{label}<select value={String(proposal[key] ?? '')} onChange={(event) => chooseConflict(key, event.target.value)}>{values.map(({ value, file }, index) => <option value={value} key={`${file}-${index}`}>{value} — {file}</option>)}</select></label>)}</section>}
          {editing ? <ProposalEditor proposal={proposal} onChange={(value) => { setProposal(value); setConfirmed(false); setSaved(null); setResolvedConflicts(conflicts.map(({ key }) => key)) }} onCancel={() => setEditing(false)} onConfirm={confirm} /> : <div className="proposal-data">
            {textFields.map(([key, label]) => <DataField key={key} label={label} value={String(proposal[key] || 'Не найдено')} evidence={uploads.flatMap(({ metadata }) => asEvidence(metadata.fieldEvidence?.[key]))} />)}
            <DataField label="Итог по позициям" value={money(total, proposal.currency)} />
            {proposal.documentTotal > 0 && <DataField label="Итог из документа" value={money(proposal.documentTotal, proposal.currency)} />}
            {proposal.items.length > 0 && <div className="items-data"><h3>Позиции</h3>{proposal.items.map((item, index) => <div className={`item-data ${duplicateItems.includes(item) ? 'duplicate-item' : ''}`} key={`${item.name}-${index}`}><span>{item.name || 'Без названия'}{duplicateItems.includes(item) && <small> Возможный дубль из нескольких файлов</small>}</span><span>{item.quantity} {item.unit}</span><strong>{money(item.quantity * item.unitPrice, proposal.currency)}</strong></div>)}<div className="total-data"><span>Итого</span><strong>{money(total, proposal.currency)}</strong></div>{proposal.documentTotal > 0 && Math.abs(total - proposal.documentTotal) > 0.01 && <p className="validation-note">Сумма позиций отличается от суммы в документе на {money(Math.abs(total - proposal.documentTotal), proposal.currency)}. Проверьте НДС, скидку и доставку.</p>}</div>}
            {proposal.notes && <DataField label="Извлеченный текст документа" value={proposal.notes} multiline />}
            <div className="validation-box"><strong>{canConfirm ? 'Обязательные данные заполнены' : 'Что нужно проверить'}</strong>{validation.length > 0 && <ul>{validation.map((message) => <li key={message}>{message}</li>)}</ul>}</div>
            <div className="data-actions"><button className="outline-cta" onClick={() => download('json')}>Скачать JSON</button><button className="outline-cta" onClick={() => download('csv')}>Скачать CSV</button><button className="outline-cta" onClick={() => setEditing(true)}>Изменить данные</button>{!confirmed && <button className="primary-cta" disabled={!canConfirm} onClick={() => setConfirmed(true)}>Подтвердить данные</button>}{confirmed && !saved && <button className="primary-cta" disabled={submitting} onClick={() => void send()}>{submitting ? 'Сохраняем…' : 'Сохранить на сервере'}</button>}</div>
            {saved && <div className="saved-message" role="status">Предложение сохранено · № {saved.id} · {new Date(saved.createdAt).toLocaleString('ru-RU')}</div>}
          </div>}
        </>}
      </section>
    </main>
  )
}

function SourcePreview({ upload }: { upload: Upload }) {
  const [url, setUrl] = useState('')
  const previewable = upload.file.type === 'application/pdf' || upload.file.type.startsWith('image/')
  useEffect(() => {
    if (!previewable) return
    const objectUrl = URL.createObjectURL(upload.file)
    setUrl(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [previewable, upload.file])
  return <section className="source-preview"><div><strong>{upload.file.name}</strong><span>{(upload.file.size / 1024 / 1024).toFixed(2)} МБ · {upload.metadata.parser}</span></div>{url && (upload.file.type === 'application/pdf' ? <iframe className="document-frame" title={`Просмотр ${upload.file.name}`} src={url} /> : <img className="document-image" src={url} alt={`Предпросмотр ${upload.file.name}`} />)}{upload.proposal.notes && <pre>{upload.proposal.notes}</pre>}{!upload.proposal.notes && !previewable && <pre>{upload.error || 'Текстовый слой недоступен; обработано с помощью OCR.'}</pre>}{upload.metadata.warnings.map((warning) => <p className="validation-note" key={warning}>{warning}</p>)}</section>
}

function ProposalEditor({ proposal, onChange, onCancel, onConfirm }: { proposal: CommercialProposal; onChange: (proposal: CommercialProposal) => void; onCancel: () => void; onConfirm: () => void }) {
  const update = <K extends keyof CommercialProposal>(key: K, value: CommercialProposal[K]) => onChange({ ...proposal, [key]: value })
  const updateItem = (index: number, key: 'name' | 'quantity' | 'unit' | 'unitPrice', value: string) => {
    const items = proposal.items.map((item, itemIndex) => itemIndex === index ? { ...item, [key]: key === 'name' || key === 'unit' ? value : Number(value) } : item)
    update('items', items)
  }
  return <div className="proposal-editor"><div className="editor-grid">{textFields.map(([key, label]) => <label key={key}>{label}<input value={String(proposal[key] || '')} onChange={(event) => update(key, event.target.value as never)} /></label>)}</div>
    <div className="editor-items-heading"><h3>Позиции</h3><button className="text-button" onClick={() => update('items', [...proposal.items, { name: '', quantity: 1, unit: 'шт.', unitPrice: 0 }])}>＋ Добавить</button></div>
    {proposal.items.map((item, index) => <div className="editor-item" key={`${item.name}-${index}`}><input aria-label="Наименование" placeholder="Наименование" value={item.name} onChange={(event) => updateItem(index, 'name', event.target.value)} /><input aria-label="Количество" type="number" min="0" step="any" value={item.quantity} onChange={(event) => updateItem(index, 'quantity', event.target.value)} /><input aria-label="Единица измерения" value={item.unit} onChange={(event) => updateItem(index, 'unit', event.target.value)} /><input aria-label="Цена за единицу" type="number" min="0" step="any" value={item.unitPrice} onChange={(event) => updateItem(index, 'unitPrice', event.target.value)} /><button className="remove-item" aria-label="Удалить позицию" onClick={() => update('items', proposal.items.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>)}
    <label className="notes-label">Текст документа<textarea rows={6} value={proposal.notes} onChange={(event) => update('notes', event.target.value)} /></label><label className="notes-label">Итог из документа<input type="number" min="0" step="any" value={proposal.documentTotal || ''} onChange={(event) => update('documentTotal', Number(event.target.value))} /></label>
    <div className="data-actions"><button className="outline-cta" onClick={onCancel}>Отмена</button><button className="primary-cta" onClick={onConfirm}>Сохранить изменения</button></div>
  </div>
}

function DataField({ label, value, multiline = false, evidence = [] }: { label: string; value: string; multiline?: boolean; evidence?: FieldEvidence[] }) {
  return <div className={`data-field ${multiline ? 'multiline' : ''}`}><span>{label}</span><strong>{value}</strong>{evidence.map((source, index) => <small className={source.verifiedInSource ? 'evidence' : 'evidence unverified'} key={`${source.file}-${index}`}>{source.file}: «{source.excerpt}»{source.verifiedInSource ? '' : ' · точное совпадение не найдено'}</small>)}</div>
}

export default App
