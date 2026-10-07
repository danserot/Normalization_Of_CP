"""Explicit extraction limits. Missing values remain missing, never guessed."""
FIELD_LABELS = {
    'title': 'Название', 'client': 'Заказчик', 'clientContact': 'Контакт заказчика',
    'validUntil': 'Срок действия', 'supplier': 'Поставщик', 'currency': 'Валюта',
    'vat': 'НДС', 'discount': 'Скидка', 'delivery': 'Стоимость доставки',
    'paymentTerms': 'Условия оплаты', 'deliveryTerms': 'Сроки поставки', 'warranty': 'Гарантия',
    'documentNumber': 'Номер документа', 'documentDate': 'Дата документа', 'documentTotal': 'Итог документа',
}


def extraction_outcome(proposal, verification):
    unreadable = verification.get('visionRequired') and not verification.get('visionComplete',
        verification.get('visionStatus') == 'vision')
    reviewed = verification.get('reviewCompleted') is True and not verification.get('semanticReviewRequired')
    reason = 'unreadable' if unreadable else 'absent' if reviewed else 'unverified'
    unavailable = [{'field': key, 'label': label, 'reason': reason} for key, label in FIELD_LABELS.items()
                   if proposal.get(key) in (None, '')]
    items = proposal.get('items', [])
    if not items:
        unavailable.append({'field': 'items', 'label': 'Товарные позиции', 'reason': reason})
    for index, item in enumerate(items):
        component_totals = item.get('components') and all(
            component.get('lineTotal') is not None for component in item['components'])
        required = [('name', 'наименование')] if component_totals else [
            ('name', 'наименование'), ('quantity', 'количество'), ('unit', 'единица измерения'),
            ('unitPrice', 'цена за единицу'), ('lineTotal', 'сумма строки')]
        for key, label in required:
            if item.get(key) in (None, ''):
                unavailable.append({'field': f'items.{index}.{key}', 'label': f'Позиция {index + 1}: {label}', 'reason': reason})
    if verification.get('unclaimedRows'):
        unavailable.append({'field': 'unclaimedRows', 'label': 'Часть строк документа', 'reason': 'unverified'})
    if verification.get('semanticReviewRequired'):
        unavailable.append({'field': 'semanticReview', 'label': 'Проверка смысловых ролей и конфликтов', 'reason': 'unverified'})
    elif not reviewed:
        unavailable.append({'field': 'semanticReview', 'label': 'Проверка данных через OpenAI не завершена', 'reason': 'unverified'})
    if verification.get('embeddedImagesReviewRequired'):
        unavailable.append({'field': 'embeddedImages', 'label': 'Встроенные изображения XLS', 'reason': 'unverified'})
    if verification.get('coverageComplete') is False and not verification.get('unclaimedRows'):
        unavailable.append({'field': 'coverage', 'label': 'Полнота чтения документа', 'reason': 'unverified'})
    if verification.get('visionRequired') and not verification.get('visionComplete',
            verification.get('visionStatus') == 'vision'):
        unavailable.append({'field': 'visualStructure', 'label': 'Чтение изображений документа', 'reason': 'unreadable'})
    found = any(proposal.get(key) not in (None, '') for key in FIELD_LABELS) or bool(items)
    state = 'unavailable' if not found else 'partial' if unavailable else 'complete'
    message = {'unavailable': 'В КП не указаны данные для извлечения.' if reason == 'absent' else 'Не удалось извлечь данные.',
               'partial': 'Данные извлечены. Часть сведений не указана в КП.' if reason == 'absent' else 'Данные извлечены частично. Часть значений не удалось прочитать или подтвердить.',
               'complete': 'Данные извлечены.'}[state]
    return {'state': state, 'message': message, 'unavailable': unavailable}
