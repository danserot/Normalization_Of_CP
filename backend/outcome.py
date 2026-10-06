"""Explicit extraction limits. Missing values remain missing, never guessed."""
FIELD_LABELS = {
    'title': 'Название', 'client': 'Заказчик', 'clientContact': 'Контакт заказчика',
    'validUntil': 'Срок действия', 'supplier': 'Поставщик', 'currency': 'Валюта',
    'vat': 'НДС', 'discount': 'Скидка', 'delivery': 'Стоимость доставки',
    'paymentTerms': 'Условия оплаты', 'deliveryTerms': 'Сроки поставки', 'warranty': 'Гарантия',
    'documentNumber': 'Номер документа', 'documentDate': 'Дата документа', 'documentTotal': 'Итог документа',
}


def extraction_outcome(proposal, verification):
    unavailable = [{'field': key, 'label': label} for key, label in FIELD_LABELS.items()
                   if proposal.get(key) in (None, '')]
    items = proposal.get('items', [])
    if not items:
        unavailable.append({'field': 'items', 'label': 'Товарные позиции'})
    for index, item in enumerate(items):
        for key, label in [('name', 'наименование'), ('quantity', 'количество'), ('unit', 'единица измерения'),
                           ('unitPrice', 'цена за единицу'), ('lineTotal', 'сумма строки')]:
            if item.get(key) in (None, ''):
                unavailable.append({'field': f'items.{index}.{key}', 'label': f'Позиция {index + 1}: {label}'})
    if verification.get('unclaimedRows'):
        unavailable.append({'field': 'unclaimedRows', 'label': 'Часть строк документа'})
    found = any(proposal.get(key) not in (None, '') for key in FIELD_LABELS) or bool(items)
    state = 'unavailable' if not found else 'partial' if unavailable else 'complete'
    message = {'unavailable': 'Не удалось извлечь данные.',
               'partial': 'Данные извлечены частично. Не удалось извлечь некоторые значения.',
               'complete': 'Данные извлечены.'}[state]
    return {'state': state, 'message': message, 'unavailable': unavailable}
