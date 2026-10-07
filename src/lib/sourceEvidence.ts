/** Human-readable origin labels shared by extraction and annotation views. */
export const sourceMethodLabel = (method?: string): string => {
  if (!method) return "";
  if (method.includes("+model"))
    return `${sourceMethodLabel(method.replace("+model", ""))} · смысл определён моделью`;
  if (method.endsWith("+rule"))
    return `${sourceMethodLabel(method.slice(0, -5))} · назначение по правилам`;
  return (
    {
      native: "исходный текст",
      "pdf-native": "исходный текст PDF",
      "docx-native": "структура Word",
      "xlsx-native": "ячейка Excel",
      "openai-vision": "текст, прочитанный ChatGPT",
      "openai-vision+native": "исходный текст · чтение ChatGPT",
      openai: "смысл определён ChatGPT",
      openpyxl: "ячейка Excel",
      "python-docx": "структура Word",
      "pdf-native+vision": "исходный текст PDF · визуальная структура",
      "paddleocr-vl": "визуальное чтение PDF",
      "paddleocr-vl+native": "исходный текст PDF · визуальная структура",
      vision: "визуальное чтение",
      model: "смысл определён моделью",
      ocr: "распознанный текст",
      manual: "проверено вручную",
    } as Record<string, string>
  )[method] ?? method;
};
