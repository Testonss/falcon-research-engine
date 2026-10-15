"""Falcon Research Engine — autonomous quantitative research framework."""

# Protocol version: bump whenever evaluation-affecting semantics change
# (splits, execution model, cost attribution, gates, scoring). Recorded in
# every campaign record and experiment fingerprint so results stay
# comparable only within the same protocol.
# v2 (RC3): per-market regime hard gate, max-multiplier cost stress, and
# cost/capacity multipliers folded into experiment identity. v1 experiment
# fingerprints are not comparable and will be re-evaluated once.
RESEARCH_PROTOCOL_VERSION = "2"
