"""Conservative placement bounds for original glyphs, including overhangs."""
import math


def horizontal_ink_frame(glyphs):
    return (min(min(g.origin[0],g.ink_bounds[0]) for g in glyphs),
            max(max(g.origin[0]+g.advance*g.size/1000,g.ink_bounds[2]) for g in glyphs))


def rightward_integer_shift(cursor,left):
    return math.ceil(cursor-left)
