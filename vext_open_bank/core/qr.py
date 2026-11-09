# vext_open_bank/core/qr.py
import base64
from functools import lru_cache
import io
import qrcode
from vext_open_bank.core.money import centimes_to_mad_str


def _tlv(tag: str, value: str) -> str:
    val_bytes = value.encode("utf-8")
    return f"{tag}{len(val_bytes):02d}{value}"


def _crc16_ccitt(data: bytes) -> str:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def build_emvco_payload(rib: str, beneficiary: str, centimes: int, motif: str) -> str:
    """Constructs a deterministic EMVCo / MarocPay-compatible TLV string."""
    merchant_acct = _tlv("00", "ma.bam.virement") + _tlv("01", rib)
    additional_data = _tlv("01", motif[:25]) + _tlv("08", motif[:25])
    body = (
        _tlv("00", "01")                                  # Payload Format Indicator
        + _tlv("01", "12")                                # Dynamic QR
        + _tlv("26", merchant_acct)                       # Beneficiary RIB
        + _tlv("52", "7372")                              # MCC: B2B SaaS / Computer Services
        + _tlv("53", "504")                               # Currency: 504 (MAD)
        + _tlv("54", centimes_to_mad_str(centimes))       # Exact Amount
        + _tlv("58", "MA")                                # Country Code
        + _tlv("59", beneficiary[:25])                    # Beneficiary Name (VEXT AI)
        + _tlv("60", "CASABLANCA")                        # City
        + _tlv("62", additional_data)                     # Motif / Invoice Reference
        + "6304"                                          # CRC Tag + Length
    )
    return body + _crc16_ccitt(body.encode("utf-8"))


@lru_cache(maxsize=2048)
def generate_qr_data_uri(rib: str, beneficiary: str, centimes: int, motif: str) -> str:
    """Returns a cached Base64 PNG Data URI (`data:image/png;base64,...`)."""
    payload = build_emvco_payload(rib, beneficiary, centimes, motif)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"
