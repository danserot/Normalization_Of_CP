import type { ParseResult } from './documentParser'

const backendFormats = /\.(pdf|docx|png|jpe?g|webp)$/i

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

export type SavedProposal = { id: string; createdAt: string; status: 'saved'; duplicate: boolean }

export const submitProposal = async (payload: { proposal: unknown; sources: unknown[] }): Promise<SavedProposal> => {
  const key = crypto.randomUUID()
  const response = await fetch('/api/proposals', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const result = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(result.detail || `Сервер сохранения вернул ${response.status}`)
  }
  return response.json() as Promise<SavedProposal>
}
