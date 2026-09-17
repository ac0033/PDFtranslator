"""Apply original-offset PDF stream edits in one linear pass."""
from .source_preserving import SourcePreservingUnsupported


def apply_stream_edits(stream,edits):
    if not edits:return stream
    cursor=0;parts=[];view=memoryview(stream)
    for start,end,value in sorted(edits):
        if not 0<=cursor<=start<=end<=len(stream):
            raise SourcePreservingUnsupported('invalid or overlapping source stream edits')
        parts.extend((view[cursor:start],value));cursor=end
    parts.append(view[cursor:])
    return b''.join(parts)
