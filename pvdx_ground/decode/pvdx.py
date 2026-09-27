"""PVDX telemetry decoder - **STUB**, the on-air format is not final.

Planned downlink framing (subject to change by the PVDX flight software team):

    raw demodulated bytes
      -> USLP transfer frame (CCSDS 732.1-B)   : primary header, optional insert zone, TFDF, OCF, FECF
      -> Space Packet(s) (CCSDS 133.0-B, SPP)  : packet primary header (APID, seq count, length)
      -> telemetry fields per APID             : layout TBD (housekeeping, EPS, ADCS, payload, ...)

Replace ``PvdxDecoder.decode`` with the real parser once the packet definitions exist. Keep the return
shape ``dict[str, float | int | str]`` so the storage stage and Grafana dashboard need no changes.
"""

from __future__ import annotations

from pvdx_ground.decode import DecodeError, Fields

# TODO(PVDX): fill in once the flight software team freezes the USLP/SPP configuration.
USLP_TFVN = 0b1100  # USLP transfer frame version number (fixed by the standard)
PVDX_SCID: int | None = None  # spacecraft identifier assigned to PVDX
PVDX_APIDS: dict[int, str] = {}  # e.g. {0x001: "housekeeping", 0x010: "eps", 0x020: "adcs"}


class PvdxDecoder:
    """Placeholder that refuses every frame until the PVDX format is implemented."""

    name = "pvdx"

    def decode(self, raw: bytes) -> Fields:
        # TODO(PVDX) step 1: parse the USLP primary header (TFVN, SCID, source/dest, VCID, MAP ID,
        #                    end-of-primary-header flag, frame length, bypass/protocol-control flags).
        # TODO(PVDX) step 2: validate the FECF (CRC-16) if enabled, then slice the TFDF.
        # TODO(PVDX) step 3: extract one or more Space Packets from the TFDF (handle spanning if used).
        # TODO(PVDX) step 4: dispatch on APID to a per-packet field layout and return a flat dict,
        #                    e.g. {"eps_batt_v": 7.92, "obc_temp_c": 21.5, "mode": "SAFE"}.
        raise DecodeError(
            "PVDX decoder not implemented: the USLP -> SPP telemetry format is TBD "
            "(see pvdx_ground/decode/pvdx.py)"
        )
