"""Archived source-fixture normalization; active Responses checks are separate."""
import json
from io import BytesIO

import pytest
from PIL import Image

from backend.vision import RenderedPage, normalize_paddle_result


def page():
    stream = BytesIO()
    Image.new('RGB', (200, 100), 'red').save(stream, format='PNG')
    return RenderedPage(7, stream.getvalue(), 200, 100, 100, 50)


def paddle_payload():
    return {'res': {'parsing_res_list': [
        {'block_label': 'table', 'block_bbox': [0, 20, 200, 100], 'block_id': 3,
         'block_order': 1, 'block_content': '<table><tr><td>1 250 000 ₸</td></tr></table>'},
        {'block_label': 'doc_title', 'block_bbox': [10, 0, 190, 20], 'block_id': 1,
         'block_order': 0, 'block_content': 'Коммерческое предложение'},
    ]}}


def test_official_result_json_and_pixel_to_pdf_coordinates():
    class Result:
        json = json.dumps(paddle_payload())
    result = normalize_paddle_result(Result(), page())
    assert result.page == 7
    assert [b.type for b in result.blocks] == ['doc_title', 'table']
    assert result.blocks[1].bbox == [0, 10, 100, 50]
    assert result.blocks[1].cell_boxes == []  # no invented cell geometry
    assert '1 250 000 ₸' in result.blocks[1].html


def test_paddlex_361_actual_block_objects_are_normalized():
    class PaddleOCRVLBlock:
        label, bbox, content = 'table', [0, 20, 200, 100], '<table><tr><td>100</td></tr></table>'
        global_block_id = None
    result = normalize_paddle_result({'res': {'parsing_res_list': [PaddleOCRVLBlock()]}}, page())
    assert result.blocks[0].html.startswith('<table>')
    assert result.blocks[0].bbox == [0, 10, 100, 50]
    assert result.blocks[0].id == 'vision-p7-b0'
@pytest.mark.parametrize('bbox', [[0, 0, float('nan'), 1], [2, 0, 1, 1], [500, 0, 600, 1]])
def test_bad_official_geometry_is_rejected(bbox):
    data = paddle_payload()
    data['res']['parsing_res_list'][0]['block_bbox'] = bbox
    with pytest.raises(ValueError):
        normalize_paddle_result(data, page())
