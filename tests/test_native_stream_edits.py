import pytest
from pdf_translation.native_stream_edits import apply_stream_edits
from pdf_translation.source_preserving import SourcePreservingUnsupported


def test_linear_splicing_matches_original_offset_replacement():
    original=bytes(range(256))*100
    edits=[(i,i+3,b'replacement') for i in range(10,len(original)-10,251)]
    edits.extend([(0,0,b'prefix'),(len(original),len(original),b'suffix'),(10,10,b'insert')])
    expected=original
    for a,b,value in sorted(edits,reverse=True):expected=expected[:a]+value+expected[b:]
    assert apply_stream_edits(original,edits)==expected


def test_dense_vector_stream_splicing_does_not_drop_unedited_bytes():
    original=b'0 0 m 1 1 l S '*20000
    edits=[(i+12,i+13,b'n') for i in range(0,len(original),14)]
    result=apply_stream_edits(original,edits)
    assert result==b'0 0 m 1 1 l n '*20000


def test_overlapping_or_outside_edits_are_rejected():
    for edits in [[(0,3,b'x'),(2,4,b'y')],[(-1,1,b'x')],[(0,6,b'x')]]:
        with pytest.raises(SourcePreservingUnsupported):apply_stream_edits(b'hello',edits)
