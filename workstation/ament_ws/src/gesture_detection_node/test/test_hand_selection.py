from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from gesture_detection_node.gesture_processor import GestureProcessor, GestureResult


@pytest.mark.parametrize('other_confidence',[.1,.8,.99])
@pytest.mark.parametrize('reversed_order',[False,True])
def test_other_hand_never_overrides_valid_thumb(other_confidence,reversed_order):
    processor=object.__new__(GestureProcessor)
    processor._rotate_image=lambda image:image
    processor.hands=Mock()
    processor.hands.process.return_value=SimpleNamespace(multi_hand_landmarks=[object(),object()])
    candidates=[GestureResult('',other_confidence,'fingers_not_folded'),GestureResult('thumbs_up',.75)]
    processor.classifier=Mock()
    processor.classifier.classify.side_effect=candidates[::-1] if reversed_order else candidates
    result,_=processor.process_hands(np.zeros((30,30,3),dtype=np.uint8))
    assert result.name=='thumbs_up' and result.confidence==.75
