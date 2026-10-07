from backend.semantic import classify_label, normalize_label

def test_aliases_and_normalization():
    assert classify_label('Кол-во')[0] == 'quantity'
    assert classify_label('Quantity')[0] == 'quantity'
    assert classify_label('Стоимость единицы')[0] == 'unitPrice'
    assert classify_label('Unit cost')[0] == 'unitPrice'
    assert normalize_label('  Цена / ед.: ') == 'цена ед'

def test_alternative_headers():
    assert classify_label('Количество товара')[0] == 'quantity'
    assert classify_label('Стоимость 1 ед.')[0] == 'unitPrice'
    assert classify_label('Общая сумма')[0] == 'lineTotal'
