import base64
import json
from datetime import date

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.ingest import chunk
from app.ingest.extract import extract


def test_split_short_text_is_one_chunk():
    assert chunk.split("hello world", 100, 10) == ["hello world"]


def test_split_long_text_overlaps_and_covers_everything():
    text = " ".join(f"Sentence number {i}." for i in range(400))
    parts = chunk.split(text, 500, 100)
    assert len(parts) > 5
    assert all(len(p) <= 500 for p in parts)
    assert "Sentence number 0." in parts[0] and "Sentence number 399." in parts[-1]


def test_extract_docx_xlsx_csv(tmp_path):
    import docx
    from openpyxl import Workbook

    d = docx.Document()
    d.add_paragraph("Refunds are approved by the shift manager.")
    d.save(tmp_path / "policy.docx")
    assert "shift manager" in extract(tmp_path / "policy.docx")[0][0]

    wb = Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(["Product", "Price"])
    ws.append(["Boiler 80L", 1990])
    wb.save(tmp_path / "prices.xlsx")
    text, loc = extract(tmp_path / "prices.xlsx")[0]
    assert "Product: Boiler 80L; Price: 1990" in text and loc == "sheet Prices"

    (tmp_path / "s.csv").write_text("name,phone\nDana,050-1234567\n", encoding="utf-8")
    assert "name: Dana; phone: 050-1234567" in extract(tmp_path / "s.csv")[0][0]


def _license(key: Ed25519PrivateKey, expires: str) -> str:
    payload = json.dumps({"customer": "Acme", "expires": expires, "max_users": 5}).encode()
    enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return enc(payload) + "." + enc(key.sign(payload))


def test_license_states(tmp_path, monkeypatch):
    from app import license
    from app.config import settings

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    lic_file = tmp_path / "license.key"
    monkeypatch.setattr(settings, "license_public_key", pub)
    monkeypatch.setattr(settings, "license_file", lic_file)
    monkeypatch.setattr(settings, "license_grace_days", 14)

    assert license.status().state == "missing"
    lic_file.write_text(_license(key, "2030-01-31"))
    st = license.status(date(2030, 1, 1))
    assert st.state == "valid" and st.max_users == 5 and st.allows_use
    assert license.status(date(2030, 2, 5)).state == "grace"
    assert not license.status(date(2030, 3, 1)).allows_use

    # Signed by someone else -> invalid
    lic_file.write_text(_license(Ed25519PrivateKey.generate(), "2030-01-31"))
    assert license.status(date(2030, 1, 1)).state == "invalid"
    # Tampered payload -> invalid
    good = _license(key, "2030-01-31")
    fake_payload = base64.urlsafe_b64encode(b'{"customer":"Acme","expires":"2099-01-01"}').decode().rstrip("=")
    lic_file.write_text(fake_payload + "." + good.split(".")[1])
    assert license.status(date(2030, 1, 1)).state == "invalid"
