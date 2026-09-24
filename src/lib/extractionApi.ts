import type { ParseResult } from './documentParser'

const backendFormats = /\.(pdf|docx|png|jpe?g|webp)$/i

export const canUseExtractionApi = (file: File) => backendFormats.test(file.name)

export type ExtractionModel = {
  id: string
  name: string
  description: string
  size: string
  recommended?: boolean
  installed?: boolean
}

const notifyUnauthorized = (status: number) => {
  if (status === 401) window.dispatchEvent(new Event('readdocument-auth-required'))
}

export const getExtractionModels = async (): Promise<ExtractionModel[]> => {
  const response = await fetch('/api/models', { credentials: 'include', signal: AbortSignal.timeout(4000) })
  if (!response.ok) {
    notifyUnauthorized(response.status)
    throw new Error('Не удалось получить список моделей')
  }
  const payload = await response.json() as { models: ExtractionModel[] }
  return payload.models
}

export const parseWithExtractionApi = async (file: File, models: string[]): Promise<ParseResult> => {
  const body = new FormData()
  body.append('file', file)
  body.append('models', JSON.stringify(models))
  const response = await fetch('/api/extract', { method: 'POST', body, credentials: 'include' })
  if (!response.ok) {
    notifyUnauthorized(response.status)
    const payload = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(payload.detail || `Сервер распознавания вернул ${response.status}`)
  }
  return response.json() as Promise<ParseResult>
}

export type SavedProposal = { id: string; createdAt: string; status: 'saved'; duplicate: boolean }

export const submitProposal = async (payload: { proposal: unknown; sources: unknown[] }, key: string): Promise<SavedProposal> => {
  const response = await fetch('/api/proposals', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    notifyUnauthorized(response.status)
    const result = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(result.detail || `Сервер сохранения вернул ${response.status}`)
  }
  return response.json() as Promise<SavedProposal>
}
