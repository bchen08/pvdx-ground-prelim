"""Pluggable frame decoders.

A decoder turns the raw bytes of one demodulated frame into a flat mapping of telemetry fields::

    decode(raw_bytes) -> dict[str, float | int | str]

Implementations ship here:

* ``satnogs:<struct>`` - wraps a compiled Kaitai struct from the ``satnogs-decoders`` package
  (the stand-in satellite path; see :mod:`pvdx_ground.decode.satnogs_kaitai`).
* ``pvdx``             - the PVDX decoder (USLP -> SPP -> fields), currently a clearly marked stub
  (see :mod:`pvdx_ground.decode.pvdx`).

Select one with the ``DECODER`` setting or ``pvdx-decode --decoder``.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pvdx_ground.ingest.state import StoredFrame

log = logging.getLogger(__name__)

FieldValue = float | int | str
Fields = dict[str, FieldValue]


class DecodeError(ValueError):
    """The frame could not be decoded (corrupt, truncated, wrong satellite, ...)."""


@runtime_checkable
class Decoder(Protocol):
    """Interface every decoder implements."""

    name: str

    def decode(self, raw: bytes) -> Fields:
        """Decode one frame; raise :class:`DecodeError` when the bytes are not a valid frame."""


@dataclass(frozen=True)
class DecodedFrame:
    """A frame plus its decoded fields, ready for storage."""

    frame: StoredFrame
    decoder: str
    fields: Fields


def normalise_fields(values: Mapping[str, Any]) -> Fields:
    """Coerce a decoder's raw output into ``dict[str, float | int | str]``.

    bools become ints, enums their names, bytes a hex string; ``None`` and non-scalar values
    (lists, nested structs) are dropped with a debug log line.
    """
    out: Fields = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, enum.Enum):
            value = value.name
        if isinstance(value, bool):
            out[key] = int(value)
        elif isinstance(value, (int, float)):
            if isinstance(value, float) and value != value:  # NaN
                continue
            out[key] = value
        elif isinstance(value, str):
            out[key] = value
        elif isinstance(value, (bytes, bytearray)):
            out[key] = bytes(value).hex()
        else:
            log.debug("dropping non-scalar field %s (%s)", key, type(value).__name__)
    return out


_REGISTRY: dict[str, Callable[[], Decoder]] = {}


def register(name: str, factory: Callable[[], Decoder]) -> None:
    """Register a decoder factory under ``name`` (used by ``get_decoder``)."""
    _REGISTRY[name] = factory


def get_decoder(spec: str | None) -> Decoder:
    """Resolve a decoder spec: ``satnogs:<struct>``, ``pvdx`` or a registered name."""
    if not spec:
        raise DecodeError("no decoder configured: set DECODER (e.g. satnogs:geoscan) or pass --decoder")
    spec = spec.strip()
    if spec.startswith("satnogs:"):
        from pvdx_ground.decode.satnogs_kaitai import SatnogsKaitaiDecoder

        return SatnogsKaitaiDecoder(spec.split(":", 1)[1])
    if spec == "pvdx":
        from pvdx_ground.decode.pvdx import PvdxDecoder

        return PvdxDecoder()
    if spec in _REGISTRY:
        return _REGISTRY[spec]()
    raise DecodeError(f"unknown decoder {spec!r}; expected satnogs:<struct>, pvdx or one of {sorted(_REGISTRY)}")


__all__ = [
    "DecodeError",
    "DecodedFrame",
    "Decoder",
    "FieldValue",
    "Fields",
    "get_decoder",
    "normalise_fields",
    "register",
]
