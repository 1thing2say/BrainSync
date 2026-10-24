"""BrainChess: play a whole game of chess with a two-target SSVEP decoder.

The flicker is drawn on the chess board itself. Each pick splits the candidate squares
into two groups on either side of a straight cut, one flickering at 15 Hz and one at
17 Hz; the user keeps looking at the piece (then the square) they want and the decoder
names its group, until one move is left. Run ``python -m brainsync.brainchess``.
"""
