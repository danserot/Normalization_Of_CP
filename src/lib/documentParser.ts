export type ParserStatus = 'parsed' | 'unsupported' | 'empty' | 'error'
export type ProposalItem = { name: string; quantity: number; unit: string; unitPrice: number }
export type CommercialProposal = {
  title: string; client: string; clientContact: string; validUntil: string; supplier: string; currency: string
  vat: string; discount: string; delivery: string; paymentTerms: string; deliveryTerms: string; warranty: string
  documentNumber: string; documentDate: string; documentTotal: number; notes: string; items: ProposalItem[]
}
export type FieldEvidence = { file: string; excerpt: string; verifiedInSource: boolean; page?: number }
export type ModelRun = {
  model: string
  name: string
  status: 'parsed' | 'error'
  durationMs: number
  proposal?: Partial<CommercialProposal>
  confidence: number
  error?: string
}
export type ExtractionMetadata = {
  sourceName: string; parser: string; status: ParserStatus; confidence: number; warnings: string[]
  fieldEvidence?: Record<string, FieldEvidence | FieldEvidence[]>; ocrPages?: number
  ocrPageNumbers?: number[]
  modelRuns?: ModelRun[]
}
export type ParseResult = { proposal: Partial<CommercialProposal>; metadata: ExtractionMetadata }
export interface DocumentParser { canParse(file: File): boolean; parse(file: File): Promise<ParseResult> }

const emptyResult = (file: File, parser: string, status: ParserStatus, warning: string): ParseResult => ({
  proposal: {}, metadata: { sourceName: file.name, parser, status, confidence: 0, warnings: [warning] },
})
const parseNumber = (value: unknown): number => {
  const raw = String(value ?? '').replace(/\s/g, '').replace(/[^\d,.-]/g, '')
  const normalized = raw.includes(',') && raw.includes('.') ? raw.replace(/\./g, '').replace(',', '.') : raw.replace(',', '.')
  const parsed = Number(normalized)
  return Number.isFinite(parsed) ? parsed : 0
}
const linesOf = (text: string) => text.split(/\r?\n/).map((line) => line.replace(/[^\S\t]+/g, ' ').trim()).filter(Boolean)
const valueAfter = (lines: string[], pattern: RegExp) => {
  const line = lines.find((candidate) => pattern.test(candidate))
  return line?.replace(pattern, '').replace(/^[\s:;,-]+/, '').trim() ?? ''
}

const parseItems = (lines: string[]): ProposalItem[] => {
  const header = /наименование|название|описание|товар|услуга|кол-во|количество|цена|стоимость|сумма/i
  return lines.flatMap((line) => {
    if (header.test(line) && !/\d/.test(line)) return []
    const cells = line.split(/\t+|\s{2,}|\|/).map((cell) => cell.trim()).filter(Boolean)
    if (cells.length < 3) return []
    const numeric = cells.map((cell, index) => ({ cell, index, value: parseNumber(cell), numeric: /\d/.test(cell) })).filter(({ numeric }) => numeric)
    if (numeric.length < 2) return []
    const quantity = numeric[0]
    const price = numeric[numeric.length - 1]
    const name = cells.slice(0, quantity.index).join(' ').trim()
    if (!name || header.test(name)) return []
    const unitCell = cells[quantity.index + 1]
    return [{ name, quantity: quantity.value, unit: unitCell && !/\d/.test(unitCell) && unitCell.length < 16 ? unitCell : 'шт.', unitPrice: price.value }]
  })
}

const parseProposalText = (text: string): Partial<CommercialProposal> => {
  const lines = linesOf(text)
  const title = lines.find((line) => /коммерческ|предложени|прайс|расценк/i.test(line)) ?? lines[0] ?? ''
  const totalLine = lines.find((line) => /итого|всего|к оплате|общая сумма/i.test(line)) ?? ''
  return {
    title,
    client: valueAfter(lines, /^(?:клиент|заказчик|покупатель|организация|компания)\b/i),
    clientContact: valueAfter(lines, /^(?:контакт|телефон|тел\.?|email|e-mail)\b/i),
    validUntil: valueAfter(lines, /^(?:срок действия|действительно до|срок предложения)\b/i),
    supplier: valueAfter(lines, /^(?:поставщик|исполнитель|продавец)\b/i),
    currency: (text.match(/\b(RUB|USD|EUR|KZT|TRY|₽|руб\.?|доллар(?:ов)?|евро)\b/i)?.[0] ?? ''),
    vat: valueAfter(lines, /^(?:ндс|налог)\b/i),
    discount: valueAfter(lines, /^(?:скидка)\b/i),
    delivery: valueAfter(lines, /^(?:доставка|стоимость доставки)\b/i),
    paymentTerms: valueAfter(lines, /^(?:условия оплаты|оплата)\b/i),
    deliveryTerms: valueAfter(lines, /^(?:срок поставки|сроки поставки)\b/i),
    warranty: valueAfter(lines, /^(?:гарантия|гарантийный срок)\b/i),
    documentNumber: valueAfter(lines, /^(?:номер|№ документа)\b/i),
    documentDate: valueAfter(lines, /^(?:дата|от)\b/i),
    documentTotal: parseNumber(totalLine),
    notes: text,
    items: parseItems(lines),
  }
}

const resultFor = (file: File, parser: string, proposal: Partial<CommercialProposal>): ParseResult => {
  const hasData = Object.entries(proposal).some(([key, value]) => key !== 'notes' && key !== 'title' && (typeof value === 'string' ? !!value : Array.isArray(value) ? value.length > 0 : !!value))
  if (!hasData) return emptyResult(file, parser, 'empty', 'Не удалось уверенно выделить поля или позиции')
  return {
    proposal,
    metadata: { sourceName: file.name, parser, status: 'parsed', confidence: proposal.items?.length ? 0.72 : 0.5, warnings: ['Локальный разбор: проверьте поля и строки таблицы перед подтверждением'] },
  }
}

export class WordParser implements DocumentParser {
  canParse(file: File) { return /\.docx$/i.test(file.name) }
  async parse(file: File) {
    try {
      const mammoth = await import('mammoth')
      const arrayBuffer = await file.arrayBuffer()
      const html = await mammoth.convertToHtml({ arrayBuffer })
      const parsed = new DOMParser().parseFromString(html.value, 'text/html')
      const paragraphLines = Array.from(parsed.querySelectorAll('p')).filter((paragraph) => !paragraph.closest('table'))
        .map((paragraph) => paragraph.textContent?.trim() ?? '').filter(Boolean)
      const tableLines = Array.from(parsed.querySelectorAll('tr'))
        .map((row) => Array.from(row.querySelectorAll('th,td')).map((cell) => cell.textContent?.trim() ?? '').join('\t'))
      const text = [...paragraphLines, ...tableLines].filter(Boolean).join('\n')
      return text ? resultFor(file, 'DOCX локально', parseProposalText(text)) : emptyResult(file, 'DOCX локально', 'empty', 'В документе нет текста')
    } catch { return emptyResult(file, 'DOCX локально', 'error', 'Не удалось прочитать DOCX') }
  }
}

export class ExcelParser implements DocumentParser {
  canParse(file: File) { return /\.(xlsx|xls)$/i.test(file.name) }
  async parse(file: File) {
    try {
      const XLSX = await import('xlsx')
      const workbook = XLSX.read(await file.arrayBuffer(), { type: 'array' })
      const rows = workbook.SheetNames.flatMap((name) => XLSX.utils.sheet_to_json<unknown[]>(workbook.Sheets[name], { header: 1, defval: '' }))
      const items = rows.flatMap((row) => {
        const cells = Array.isArray(row) ? row.map((cell) => String(cell ?? '').trim()) : []
        const normalized = cells.map((cell) => cell.toLocaleLowerCase())
        if (normalized.some((cell) => /наименование|название|товар/.test(cell))) return []
        const name = cells[0] ?? ''
        if (!name) return []
        const quantity = parseNumber(cells[1])
        const unit = cells[2] && !/\d/.test(cells[2]) ? cells[2] : 'шт.'
        const priceIndex = cells.length >= 4 ? 3 : 2
        const unitPrice = parseNumber(cells[priceIndex])
        return quantity > 0 || unitPrice > 0 ? [{ name, quantity: quantity || 1, unit, unitPrice }] : []
      })
      return items.length ? resultFor(file, 'Excel локально', { title: 'Коммерческое предложение', items }) : emptyResult(file, 'Excel локально', 'empty', 'Не найдены строки с позициями')
    } catch { return emptyResult(file, 'Excel локально', 'error', 'Не удалось прочитать Excel') }
  }
}

export class PdfParser implements DocumentParser {
  canParse(file: File) { return /\.pdf$/i.test(file.name) || file.type === 'application/pdf' }
  async parse(file: File) {
    try {
      const pdfjsLib = await import('pdfjs-dist')
      pdfjsLib.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString()
      const document = await pdfjsLib.getDocument({ data: await file.arrayBuffer() }).promise
      const pages: string[] = []
      for (let number = 1; number <= document.numPages; number += 1) {
        const page = await document.getPage(number)
        const content = await page.getTextContent()
        pages.push(`[Страница ${number}]\n${content.items.map((item) => 'str' in item ? item.str : '').join(' ')}`)
      }
      const text = pages.join('\n').trim()
      return text ? resultFor(file, 'PDF локально', parseProposalText(text)) : emptyResult(file, 'PDF локально', 'empty', 'В PDF нет текстового слоя; требуется OCR сервером')
    } catch { return emptyResult(file, 'PDF локально', 'error', 'Не удалось прочитать PDF') }
  }
}

export class TextParser implements DocumentParser {
  canParse(file: File) { return /\.(txt|csv|tsv|json)$/i.test(file.name) || file.type.startsWith('text/') }
  async parse(file: File) {
    try {
      const text = (await file.text()).trim()
      return text ? resultFor(file, 'Текст локально', parseProposalText(text)) : emptyResult(file, 'Текст локально', 'empty', 'Файл пуст')
    } catch { return emptyResult(file, 'Текст локально', 'error', 'Не удалось прочитать текстовый файл') }
  }
}

const parsers: DocumentParser[] = [new WordParser(), new ExcelParser(), new PdfParser(), new TextParser()]
export const parseDocument = async (file: File): Promise<ParseResult> => {
  const parser = parsers.find((candidate) => candidate.canParse(file))
  return parser ? parser.parse(file) : emptyResult(file, 'Без парсера', 'unsupported', 'Формат файла не поддерживается')
}
