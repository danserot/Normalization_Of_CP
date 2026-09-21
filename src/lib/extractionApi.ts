import type { ParseResult } from './documentParser'

const backendFormats = /\.(pdf|docx)$/i

export const canUseExtractionApi = (file: File) => backendFormats.test(file.name)

export const parseWithExtractionApi = async (file: File): Promise<ParseResult> => {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch('/api/extract', { method: 'POST', body })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(payload.detail || `Сервер распознавания вернул ${response.status}`)
  }
  return response.json() as Promise<ParseResult>
}
