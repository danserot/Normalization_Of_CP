import * as mammoth from 'mammoth'
import * as pdfjsLib from 'pdfjs-dist'
import * as XLSX from 'xlsx'

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

export type ParserStatus = 'parsed' | 'unsupported' | 'empty' | 'error'
export type ProposalItem = { name: string; quantity: number; unit: string; unitPrice: number }
export type CommercialProposal = {
  title: string
  client: string
  clientContact: string
  validUntil: string
  notes: string
  items: ProposalItem[]
}
export type ExtractionMetadata = {
  sourceName: string
  parser: string
  status: ParserStatus
  confidence: number
  warnings: string[]
}
export type ParseResult = { proposal: Partial<CommercialProposal>; metadata: ExtractionMetadata }

export interface DocumentParser {
  canParse(file: File): boolean
  parse(file: File): Promise<ParseResult>
}

const emptyResult = (file: File, parser: string, status: ParserStatus, warning: string): ParseResult => ({
  proposal: {},
  metadata: { sourceName: file.name, parser, status, confidence: 0, warnings: [warning] },
})

const parseNumber = (value: unknown): number => {
  const parsed = Number(String(value ?? '').replace(/\s/g, '').replace(',', '.').replace(/[^\d.-]/g, ''))
  return Number.isFinite(parsed) ? parsed : 0
}

const valueAfterLabel = (lines: string[], pattern: RegExp): string => {
  const line = lines.find((candidate) => pattern.test(candidate))
  if (!line) return ''
  return line.replace(pattern, '').replace(/^[\s:;,-]+/, '').trim()
}

const parseItems = (lines: string[]): ProposalItem[] => {
  const items: ProposalItem[] = []
  const headers = /наименование|название|описание|товар|услуга|кол-во|количество|цена|стоимость|сумма/i

  for (const line of lines) {
    if (headers.test(line) && !/\d/.test(line)) continue
    const cells = line.split(/\t+|\s{2,}|\|/).map((cell) => cell.trim()).filter(Boolean)
    if (cells.length < 3) continue
    const numericCells = cells
      .map((cell, index) => ({ index, value: parseNumber(cell), hasNumber: /\d/.test(cell) }))
      .filter((cell) => cell.hasNumber)
    if (numericCells.length < 2) continue

    const quantityCell = numericCells[0]
    const priceCell = numericCells[numericCells.length - 1]
    const name = cells.slice(0, quantityCell.index).join(' ').trim()
    if (!name || headers.test(name)) continue

    const unitCell = cells[quantityCell.index + 1]
    const unit = unitCell && !/\d/.test(unitCell) && unitCell.length <= 12 ? unitCell : 'шт.'
    items.push({
      name,
      quantity: quantityCell.value || 1,
      unit,
      unitPrice: priceCell.value,
    })
  }
  return items
}

const parseProposalText = (text: string): Partial<CommercialProposal> => {
  const lines = text.split(/\r?\n/).map((line) => line.replace(/[^\S\t]+/g, ' ').trim()).filter(Boolean)
  const client = valueAfterLabel(lines, /^(?:клиент|заказчик|организация|компания)\b/i)
  const clientContact = valueAfterLabel(lines, /^(?:контакт|телефон|тел\.|email|e-mail)\b/i)
  const validUntil = valueAfterLabel(lines, /^(?:срок действия|действительно до|срок предложения)\b/i)
  const items = parseItems(lines)
  const title = lines.find((line) => /коммерческ|предложен|прайс|расценк/i.test(line)) ?? lines[0] ?? 'Коммерческое предложение'

  return { title, client, clientContact, validUntil, notes: text, ...(items.length ? { items } : {}) }
}

export class WordParser implements DocumentParser {
  canParse(file: File) { return /\.docx$/i.test(file.name) }
  async parse(file: File): Promise<ParseResult> {
    try {
      const arrayBuffer = await file.arrayBuffer()
      const result = await mammoth.extractRawText({ arrayBuffer })
      const html = await mammoth.convertToHtml({ arrayBuffer })
      const tableLines = Array.from(new DOMParser().parseFromString(html.value, 'text/html').querySelectorAll('tr'))
        .map((row) => Array.from(row.querySelectorAll('th,td')).map((cell) => cell.textContent?.trim() ?? '').join('\t'))
        .filter(Boolean)
      const text = [result.value.trim(), ...tableLines].filter(Boolean).join('\n').trim()
      if (!text) return emptyResult(file, 'WordParser', 'empty', 'В документе не найден текст')
      const proposal = parseProposalText(text)
      return {
        proposal,
        metadata: {
          sourceName: file.name,
          parser: 'WordParser',
          status: 'parsed',
          confidence: proposal.client && proposal.items?.length ? 0.9 : proposal.client ? 0.78 : 0.6,
          warnings: ['Проверьте реквизиты и позиции перед отправкой'],
        },
      }
    } catch { return emptyResult(file, 'WordParser', 'error', 'Не удалось прочитать DOCX файл') }
  }
}

export class ExcelParser implements DocumentParser {
  canParse(file: File) { return /\.(xlsx|xls)$/i.test(file.name) }
  async parse(file: File): Promise<ParseResult> {
    try {
      const workbook = XLSX.read(await file.arrayBuffer(), { type: 'array' })
      const rows = workbook.SheetNames.flatMap((name) => XLSX.utils.sheet_to_json<unknown[]>(workbook.Sheets[name], { header: 1, defval: '' }))
      const items = rows.slice(0, 30).map((row) => {
        const cells = Array.isArray(row) ? row : []
        const name = String(cells[0] ?? '').trim()
        return { name, quantity: parseNumber(cells[1]) || 1, unit: String(cells[2] || 'шт'), unitPrice: parseNumber(cells[3] ?? cells[2]) }
      }).filter((item) => item.name && !/наименование|товар|название/i.test(item.name))
      if (!items.length) return emptyResult(file, 'ExcelParser', 'empty', 'В таблице не найдены товарные позиции')
      return {
        proposal: { title: 'Коммерческое предложение', items },
        metadata: { sourceName: file.name, parser: 'ExcelParser', status: 'parsed', confidence: 0.86, warnings: ['Проверьте колонки количества и цены'] },
      }
    } catch { return emptyResult(file, 'ExcelParser', 'error', 'Не удалось прочитать Excel файл') }
  }
}

export class PdfParser implements DocumentParser {
  canParse(file: File) { return /\.pdf$/i.test(file.name) || file.type === 'application/pdf' }

  async parse(file: File): Promise<ParseResult> {
    try {
      const document = await pdfjsLib.getDocument({ data: await file.arrayBuffer() }).promise
      const pages: string[] = []

      for (let pageNumber = 1; pageNumber <= document.numPages; pageNumber += 1) {
        const page = await document.getPage(pageNumber)
        const content = await page.getTextContent()
        const pageText = content.items
          .map((item) => 'str' in item ? item.str : '')
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim()
        if (pageText) pages.push(pageText)
      }

      const text = pages.join('\n\n').trim()
      if (!text) return emptyResult(file, 'PdfParser', 'empty', 'В PDF не найден текст. Возможно, это скан без OCR')

      const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
      const clientLine = lines.find((line) => /клиент|заказчик|организац/i.test(line))
      return {
        proposal: {
          title: lines[0] ?? 'Коммерческое предложение',
          client: clientLine?.split(/[:;,]/).slice(1).join(':').trim() ?? '',
          notes: text,
        },
        metadata: {
          sourceName: file.name,
          parser: 'PdfParser',
          status: 'parsed',
          confidence: clientLine ? 0.8 : 0.62,
          warnings: ['Проверьте реквизиты и позиции перед отправкой'],
        },
      }
    } catch {
      return emptyResult(file, 'PdfParser', 'error', 'Не удалось прочитать PDF файл')
    }
  }
}

export class ImageParser implements DocumentParser {
  canParse(file: File) { return /\.(jpe?g|png|gif|webp)$/i.test(file.name) }
  async parse(file: File): Promise<ParseResult> {
    return emptyResult(file, 'ImageParser', 'unsupported', 'Распознавание изображений пока недоступно')
  }
}

export class TextParser implements DocumentParser {
  canParse(file: File) { return /\.(txt|csv|tsv|json)$/i.test(file.name) || file.type.startsWith('text/') }
  async parse(file: File): Promise<ParseResult> {
    try {
      const text = (await file.text()).trim()
      if (!text) return emptyResult(file, 'TextParser', 'empty', 'В документе нет текста')
      const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
      const clientLine = lines.find((line) => /клиент|заказчик|организац/i.test(line))
      return {
        proposal: {
          title: lines[0] ?? 'Коммерческое предложение',
          client: clientLine?.split(/[:;,]/).slice(1).join(':').trim() ?? '',
          notes: text,
        },
        metadata: {
          sourceName: file.name,
          parser: 'TextParser',
          status: 'parsed',
          confidence: clientLine ? 0.7 : 0.5,
          warnings: ['Проверьте реквизиты и позиции перед отправкой'],
        },
      }
    } catch {
      return emptyResult(file, 'TextParser', 'error', 'Не удалось прочитать текстовый файл')
    }
  }
}

const parsers: DocumentParser[] = [new WordParser(), new ExcelParser(), new PdfParser(), new TextParser(), new ImageParser()]
export const parseDocument = async (file: File): Promise<ParseResult> => {
  const parser = parsers.find((candidate) => candidate.canParse(file))
  return parser ? parser.parse(file) : emptyResult(file, 'NoParser', 'unsupported', 'Формат файла не поддерживается')
}
