"""Decoder that wraps a compiled Kaitai struct from the ``satnogs-decoders`` PyPI package.

SatNOGS DB decodes every satellite's frames with the Kaitai structs in
https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders (``ksy/``). The same project is
published on PyPI as ``satnogs-decoders`` with the structs pre-compiled to Python, so no Kaitai
compiler is needed here. Field names come from the ``:field name: path`` annotations in each struct's
docstring, exactly as SatNOGS DB reports them, via ``satnogsdecoders.decoder.get_fields``.
"""

from __future__ import annotations

import logging

from pvdx_ground.decode import DecodeError, Fields, normalise_fields

log = logging.getLogger(__name__)


def available_structs() -> dict[str, str]:
    """Map lower-case struct names (as SatNOGS DB spells them, e.g. ``geoscan``) to class names."""
    import satnogsdecoders.decoder as sd

    return {name.lower(): name for name in dir(sd) if name[:1].isupper() and isinstance(getattr(sd, name), type)}


class SatnogsKaitaiDecoder:
    """``decode(raw)`` parses ``raw`` with the named struct and flattens its documented fields."""

    def __init__(self, struct_name: str) -> None:
        import satnogsdecoders.decoder as sd

        key = struct_name.strip().lower().replace("_", "").replace("-", "")
        structs = available_structs()
        if key not in structs:
            raise DecodeError(
                f"satnogs-decoders has no struct named {struct_name!r}; try one of: "
                + ", ".join(sorted(structs)[:12])
                + ", ..."
            )
        self._get_fields = sd.get_fields
        self._struct = getattr(sd, structs[key])
        self.struct_name = structs[key].lower()
        self.name = f"satnogs:{self.struct_name}"

    def decode(self, raw: bytes) -> Fields:
        if not raw:
            raise DecodeError("empty frame")
        try:
            parsed = self._struct.from_bytes(raw)
            fields = self._get_fields(parsed)
        except DecodeError:
            raise
        except Exception as exc:  # Kaitai raises EOFError, ValidationFailedError, UnicodeDecodeError, ...
            raise DecodeError(f"{self.struct_name}: {type(exc).__name__}: {exc}") from exc
        return normalise_fields(fields)

    def __repr__(self) -> str:
        return f"SatnogsKaitaiDecoder({self.struct_name!r})"
