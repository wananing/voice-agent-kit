"""Pre-synthesised cue library (implements `CuePort`)."""

from .library import WELL_KNOWN_CUES, CueLibrary, read_wav_pcm16, synth_tone_cue

__all__ = ["WELL_KNOWN_CUES", "CueLibrary", "read_wav_pcm16", "synth_tone_cue"]
