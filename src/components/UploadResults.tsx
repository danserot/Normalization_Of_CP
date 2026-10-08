import type { ExtractionMetadata, FieldEvidence } from "../types/extraction";
import type { DocumentUpload } from "../lib/uploadProcessing";
import { readableExtractionMessage, unavailableLabel } from "../lib/uploadProcessing";

export function UploadResults({
  uploads,
  previewFile,
  onPreview,
  onRetry,
}: {
  uploads: DocumentUpload[];
  previewFile: string | null;
  onPreview: (key: string | null) => void;
  onRetry: () => void;
}) {
  const failedCount = uploads.filter((upload) => upload.error).length;
  return (
    <>
      <div className="uploaded-results" aria-label="Результаты по файлам">
        {uploads.map(({ file, metadata, error }, index) => {
          const key = `${file.name}-${index}`;
          const partial = metadata.outcome && metadata.outcome.state !== "complete";
          const message = error
            ? readableExtractionMessage(error)
            : partial
              ? metadata.outcome!.message
              : `✓ Документ прочитан${metadata.cacheHit ? " · из кэша" : ""}`;
          const warnings = error ? [] : [...new Set(metadata.warnings
            .map(readableExtractionMessage))].filter((warning) => warning !== message);
          return (
            <div className={`result-row ${error ? "result-failed" : ""}`}
              key={`${file.name}-${file.lastModified}-${index}`}>
              <div className="result-summary">
                <button className="source-link" onClick={() => onPreview(previewFile === key ? null : key)}
                  aria-expanded={previewFile === key}>
                  {file.name}
                </button>
                <span className={`status ${error || partial ? "warning" : "done"}`}
                  role={error ? "alert" : "status"}>
                  {message}
                </span>
              </div>
              <details className="result-details">
                <summary>{error ? "Сведения об обработке" : "Подробнее о результате"}</summary>
                {warnings.length > 0 && (
                  <ul className="result-warnings">
                    {warnings.map((warning) => <li key={warning}>{warning}</li>)}
                  </ul>
                )}
                {!error && <EvidenceExplanation metadata={metadata} />}
                <UsageDetails metadata={metadata} />
                {!error && metadata.routing && (
                  <p>Структура документа: {metadata.routing.used ? "проверена визуально" : "прочитана из файла"}
                    {metadata.routing.processed_pages?.length ? ` · страницы ${metadata.routing.processed_pages.join(", ")}` : ""}
                    {metadata.routing.mandatory && !metadata.routing.used ? " · требуется визуальная проверка" : ""}
                  </p>
                )}
                {metadata.timingsMs && (
                  <p>Время обработки: {Math.round(metadata.timingsMs.total / 1000)} с.</p>
                )}
                {!error && !!metadata.outcome?.unavailable.length && (
                  <details>
                    <summary>Поля без подтверждённого значения ({metadata.outcome.unavailable.length})</summary>
                    <ul>
                      {metadata.outcome.unavailable.map((entry) => (
                        <li key={entry.field}>{entry.label}: {unavailableLabel(entry.reason)}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {!error && metadata.verification
                  && !["model_direct", "model_error"].includes(metadata.verification.mode) && (
                  <p>Охват товарных строк: {metadata.verification.coverageComplete
                    ? "все строки учтены" : `не проверено строк: ${metadata.verification.unclaimedRows.length}`}
                    {" · "}визуальное чтение: {metadata.verification.visionUsed ? "выполнено" : "не выполнялось"}
                  </p>
                )}
              </details>
            </div>
          );
        })}
      </div>
      {failedCount > 0 && (
        <div className="retry-actions">
          <button className="outline-cta" onClick={onRetry}>
            {failedCount === uploads.length ? "Повторить чтение" : "Повторить для непрочитанных файлов"}
          </button>
        </div>
      )}
    </>
  );
}

function UsageDetails({ metadata }: { metadata: ExtractionMetadata }) {
  const usage = metadata.apiUsage;
  if (!usage)
    return <p className="validation-note">Расход API: статистика недоступна</p>;
  const money = (value: number | null) =>
    value === null ? "не определена" : `$${value.toFixed(6)}`;
  return (
    <details className="validation-note">
      <summary>
        {metadata.cacheHit ?
          "Из кеша · новых токенов: 0 · стоимость сейчас: $0"
        : `Токены: ${(usage.inputTokens + usage.outputTokens).toLocaleString("ru-RU")} · стоимость API ≈ ${money(usage.estimatedCostUsd)}`
        }
      </summary>
      <p>
        {metadata.cacheHit ? "Первоначальная обработка: " : ""}Вход:{" "}
        {usage.inputTokens.toLocaleString("ru-RU")} · из них кеш API:{" "}
        {usage.cachedInputTokens.toLocaleString("ru-RU")} · выход:{" "}
        {usage.outputTokens.toLocaleString("ru-RU")} · из них рассуждения:{" "}
        {usage.reasoningTokens.toLocaleString("ru-RU")}
      </p>
      <p>
        Стоимость обработки ≈ {money(usage.estimatedCostUsd)} · тариф от{" "}
        {usage.pricingDate}, USD, без налогов и сервера.
      </p>
      {!usage.complete && (
        <p>
          Учёт неполный: API не сообщил весь расход или тариф модели неизвестен.
          Итоговая стоимость не определена.
        </p>
      )}
      {Array.from(new Set(usage.calls.map((call) => call.stage))).map((stage) => {
        const calls = usage.calls.filter((call) => call.stage === stage);
        if (!calls.length) return null;
        const cost =
          calls.every((call) => call.estimatedCostUsd !== null) ?
            calls.reduce((sum, call) => sum + (call.estimatedCostUsd ?? 0), 0)
          : null;
        return (
          <p key={stage}>
            {stage === "vision" ? "Чтение изображений" : "Извлечение данных"}:{" "}
            {calls.length} запросов ·{" "}
            {calls
              .reduce(
                (sum, call) => sum + call.inputTokens + call.outputTokens,
                0,
              )
              .toLocaleString("ru-RU")}{" "}
            токенов · ≈ {money(cost)} ·{" "}
            {Array.from(new Set(calls.map((call) => call.model))).join(", ")}
          </p>
        );
      })}
    </details>
  );
}

function EvidenceExplanation({ metadata }: { metadata: ExtractionMetadata }) {
  const entries = Object.values(metadata.fieldEvidence ?? {});
  if (metadata.verification?.mode === "model_direct")
    return (
      <details className="confidence-explanation">
        <summary>Как проверить результат модели?</summary>
        <p>
          Модель читает исходный файл и возвращает данные и цитаты. Цитаты (
          {entries.length}) тоже получены моделью и не проверены независимым
          парсером.
        </p>
        <p>
          Сверьте товары, цифры и реквизиты с оригиналом. Формат ответа и
          арифметика проверяются кодом; точность распознавания не измерена.
        </p>
      </details>
    );
  const scored = entries.filter(
    (entry): entry is FieldEvidence =>
      !Array.isArray(entry) && typeof entry.confidence === "number",
  );
  const conflicts = entries.filter(Array.isArray).length;
  return (
    <details className="confidence-explanation">
      <summary>Как читать подтверждения источника?</summary>
      <p>
        Ссылка показывает, из какой ячейки или фрагмента взято значение. Это
        помогает найти его в документе, но само совпадение текста не
        гарантирует, что поле выбрано по смыслу правильно.
      </p>
      <ul>
        <li>Значений с привязанным источником: {scored.length}.</li>
        <li>Конфликтующих значений: {conflicts}.</li>
      </ul>
      <p>
        Проценты — оценка надёжности по правилам проверки источника, структуры и
        расчётов. Это эвристика backend, а не измеренная точность распознавания.
      </p>
      <p>
        Перед сохранением проверьте смысл поля, конфликты и суммы по оригиналу.
      </p>
    </details>
  );
}
