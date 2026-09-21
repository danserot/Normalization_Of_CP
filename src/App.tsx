import { useMemo, useRef, useState } from 'react'
import type { ChangeEvent, DragEvent } from 'react'
import { parseDocument } from './lib/documentParser'
import { canUseExtractionApi, parseWithExtractionApi } from './lib/extractionApi'
import type { CommercialProposal, ExtractionMetadata } from './lib/documentParser'
import './App.css'

type Upload = { file: File; metadata: ExtractionMetadata }

const blank: CommercialProposal = {
  title: 'Коммерческое предложение',
  client: '',
  clientContact: '',
  validUntil: '',
  notes: '',
  items: [],
}

const money = (value: number) =>
  new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency: 'RUB',
    maximumFractionDigits: 2,
  }).format(value)

function App() {
  const [uploads, setUploads] = useState<Upload[]>([])
  const [proposal, setProposal] = useState<CommercialProposal>(blank)
  const [error, setError] = useState('')
  const [reading, setReading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [editing, setEditing] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const total = useMemo(
    () => proposal.items.reduce((sum, item) => sum + item.quantity * item.unitPrice, 0),
    [proposal.items],
  )

  const processFiles = async (files: File[]) => {
    const accepted = files.filter((file) => file.size <= 25 * 1024 * 1024)
    if (!accepted.length) {
      setError('Выберите файлы размером не больше 25 МБ')
      return
    }

    setError('')
    setReading(true)
    try {
      const results = await Promise.all(
        accepted.map(async (file) => {
          if (canUseExtractionApi(file)) {
            try {
              return { file, result: await parseWithExtractionApi(file) }
            } catch {
              return { file, result: await parseDocument(file) }
            }
          }
          return { file, result: await parseDocument(file) }
        }),
      )
      setUploads(results.map(({ file, result }) => ({ file, metadata: result.metadata })))
      setProposal(
        results.reduce(
          (current, { result }) => ({
            ...current,
            ...result.proposal,
            items: [...current.items, ...(result.proposal.items ?? [])],
          }),
          blank,
        ),
      )
      setEditing(false)
      setConfirmed(false)
    } catch {
      setError('Не удалось прочитать выбранные файлы')
    } finally {
      setReading(false)
    }
  }

  const onInput = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) void processFiles(Array.from(event.target.files))
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    void processFiles(Array.from(event.dataTransfer.files))
  }

  const reset = () => {
    setUploads([])
    setProposal(blank)
    setError('')
    setEditing(false)
    setConfirmed(false)
    if (inputRef.current) inputRef.current.value = ''
  }

  return (
    <main className="reader">
      <header className="reader-header">
        <div>
          <span className="eyebrow">READ DOCUMENT</span>
          <h1>Чтение коммерческого предложения</h1>
          <p>Загрузите КП — сервис извлечёт данные для отправки на backend.</p>
        </div>
        {uploads.length > 0 && <button className="outline-cta" onClick={reset}>Новое чтение</button>}
      </header>

      <section className="reader-content">
        {error && <div className="alert">{error}</div>}
        {uploads.length === 0 && !reading && (
          <div
            className={`dropzone ${dragging ? 'dragging' : ''}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
          >
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".docx,.xlsx,.xls,.csv,.txt,.json,.pdf,image/*"
              onChange={onInput}
            />
            <div className="upload-icon">↑</div>
            <strong>Перетащите КП сюда</strong>
            <p>или <u>выберите файл на компьютере</u></p>
            <small>Можно загрузить несколько файлов · до 25 МБ каждый</small>
          </div>
        )}

        {reading && (
          <div className="analysis">
            <div className="spinner" />
            <h2>Читаем документ</h2>
            <p>Извлекаем текст, реквизиты, товары и цены…</p>
          </div>
        )}

        {!reading && uploads.length > 0 && (
          <>
            <div className="results-heading">
              <div>
                <h2>Данные из КП</h2>
                <p>{confirmed ? 'Данные подтверждены и готовы к отправке на backend.' : 'Проверьте результат чтения перед отправкой на backend.'}</p>
              </div>
              <div className="heading-actions">
                {confirmed && <span className="confirmed-badge">✓ Подтверждено</span>}
                <span className="file-count">{uploads.length} {uploads.length === 1 ? 'файл' : 'файла'}</span>
              </div>
            </div>

            <div className="uploaded-results">
              {uploads.map(({ file, metadata }) => (
                <div className="result-row" key={`${file.name}-${file.lastModified}`}>
                  <b>{file.name}</b>
                  <span className={`status ${metadata.status === 'parsed' ? 'done' : 'warning'}`}>
                    {metadata.status === 'parsed'
                      ? `✓ ${Math.round(metadata.confidence * 100)}%`
                      : metadata.warnings[0]}
                  </span>
                </div>
              ))}
            </div>

            {editing ? (
              <ProposalEditor
                proposal={proposal}
                onChange={(value) => { setProposal(value); setConfirmed(false) }}
                onCancel={() => setEditing(false)}
                onConfirm={() => { setEditing(false); setConfirmed(true) }}
              />
            ) : (
              <div className="proposal-data">
                <DataField label="Название" value={proposal.title} />
                <DataField label="Клиент" value={proposal.client || 'Не найден'} />
                <DataField label="Контакт" value={proposal.clientContact || 'Не найден'} />
                <DataField label="Срок действия" value={proposal.validUntil || 'Не найден'} />
                {proposal.items.length > 0 && (
                  <div className="items-data">
                    <h3>Позиции</h3>
                    {proposal.items.map((item, index) => (
                      <div className="item-data" key={`${item.name}-${index}`}>
                        <span>{item.name}</span>
                        <span>{item.quantity} {item.unit}</span>
                        <strong>{money(item.quantity * item.unitPrice)}</strong>
                      </div>
                    ))}
                    <div className="total-data"><span>Итого</span><strong>{money(total)}</strong></div>
                  </div>
                )}
                {proposal.notes && <DataField label="Текст документа" value={proposal.notes} multiline />}
                <div className="data-actions">
                  <button className="outline-cta" onClick={() => setEditing(true)}>Изменить данные</button>
                  {!confirmed && <button className="primary-cta" onClick={() => setConfirmed(true)}>Подтвердить данные</button>}
                </div>
              </div>
            )}
          </>
        )}
      </section>
    </main>
  )
}

type ProposalEditorProps = {
  proposal: CommercialProposal
  onChange: (proposal: CommercialProposal) => void
  onCancel: () => void
  onConfirm: () => void
}

function ProposalEditor({ proposal, onChange, onCancel, onConfirm }: ProposalEditorProps) {
  const update = <K extends keyof CommercialProposal>(key: K, value: CommercialProposal[K]) =>
    onChange({ ...proposal, [key]: value })
  const updateItem = (index: number, key: 'name' | 'quantity' | 'unit' | 'unitPrice', value: string) => {
    const items = proposal.items.map((item, itemIndex) => itemIndex === index
      ? { ...item, [key]: key === 'name' || key === 'unit' ? value : Number(value) || 0 }
      : item)
    update('items', items)
  }

  return (
    <div className="proposal-editor">
      <label>Название<input value={proposal.title} onChange={(event) => update('title', event.target.value)} /></label>
      <div className="editor-grid">
        <label>Клиент<input value={proposal.client} onChange={(event) => update('client', event.target.value)} /></label>
        <label>Контакт<input value={proposal.clientContact} onChange={(event) => update('clientContact', event.target.value)} /></label>
        <label>Срок действия<input value={proposal.validUntil} onChange={(event) => update('validUntil', event.target.value)} /></label>
      </div>
      <div className="editor-items-heading">
        <h3>Позиции</h3>
        <button className="text-button" onClick={() => update('items', [...proposal.items, { name: '', quantity: 1, unit: 'шт.', unitPrice: 0 }])}>＋ Добавить</button>
      </div>
      {proposal.items.map((item, index) => (
        <div className="editor-item" key={`${item.name}-${index}`}>
          <input placeholder="Наименование" value={item.name} onChange={(event) => updateItem(index, 'name', event.target.value)} />
          <input type="number" min="0" value={item.quantity} onChange={(event) => updateItem(index, 'quantity', event.target.value)} />
          <input value={item.unit} onChange={(event) => updateItem(index, 'unit', event.target.value)} />
          <input type="number" min="0" value={item.unitPrice} onChange={(event) => updateItem(index, 'unitPrice', event.target.value)} />
          <button className="remove-item" onClick={() => update('items', proposal.items.filter((_, itemIndex) => itemIndex !== index))}>×</button>
        </div>
      ))}
      <label>Текст документа<textarea rows={6} value={proposal.notes} onChange={(event) => update('notes', event.target.value)} /></label>
      <div className="data-actions">
        <button className="outline-cta" onClick={onCancel}>Отменить</button>
        <button className="primary-cta" onClick={onConfirm}>Сохранить и подтвердить</button>
      </div>
    </div>
  )
}

function DataField({ label, value, multiline = false }: { label: string; value: string; multiline?: boolean }) {
  return (
    <div className={`data-field ${multiline ? 'multiline' : ''}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export default App
