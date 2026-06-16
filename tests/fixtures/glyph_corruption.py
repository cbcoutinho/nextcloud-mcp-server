"""Shared test data for the tier-0 glyph-corruption signal.

A fast-tier text layer that looks like words -- normal spacing and token lengths,
so it scores HIGH on ``_text_quality`` -- but leaks C0 control characters: the
broken-/ToUnicode signature that ``classifier._control_char_ratio`` catches. The
alphabetic tokens decode to a pangram under a -3 (Caesar) shift.

Kept in one place so the classifier and registry tiering tests can't diverge.
"""

GLYPH_CORRUPT_TEXT = "WKH \x0f TXLFN \x10 EURZQ \x11 IRA MXPSV \x0f RYHU \x10 GRJ " * 6
