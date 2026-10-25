"""The three VEP codings of Bin et al. (2009): time (t-VEP), frequency (f-VEP) and
pseudorandom code (c-VEP) modulation, as stimulus sequences, decoders and a live demo.

- ``codes``: m-sequences, lagged code matrices and non-overlapping flash schedules.
- ``cvep``: the c-VEP template decoder (one trained template, shifted per target).
- ``tvep``: the t-VEP decoder (flash-locked epoch averages, peak-to-valley amplitude).
- ``fvep``: the paper's f-VEP decoder (FFT peak SNR), in the CCA classifier interface.
- ``sync``: cutting cycles and flash epochs out of a sample stream by marker time.
- ``__main__``: ``python -m brainsync.vep``, the demos and the comparison table.
"""
