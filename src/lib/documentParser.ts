export type ParserStatus = 'parsed' | 'unsupported' | 'empty' | 'error'
export type ProposalItem = { name: string; quantity: number | null; unit: string; unitPrice: number | null; lineTotal?: number | null }
export type CommercialProposal = {
  title: string; client: string; clientContact: string; validUntil: string; supplier: string; currency: string
  vat: string; discount: string; delivery: string; paymentTerms: string; deliveryTerms: string; warranty: string
  documentNumber: string; documentDate: string; documentTotal: number | null; notes: string; items: ProposalItem[]
}
export type FieldEvidence = { file: string; excerpt: string; verifiedInSource: boolean; page?: number; sheet?: string; block?: string; row?: number; cell?: number; method?: string; confidence?: number; warning?: string; value?: string | number }
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
