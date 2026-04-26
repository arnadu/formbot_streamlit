from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class PdfSpec:
    filename: str
    title: str
    body: str


def _pdf_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _pdf_sanitize_ascii(text: str) -> str:
    # Keep PDFs maximally portable by restricting text to ASCII.
    # Any non-ASCII characters become '?'.
    return text.encode("ascii", errors="replace").decode("ascii")


def _make_simple_pdf_bytes(title: str, body: str) -> bytes:
    # Minimal single-page PDF with Helvetica text.
    # Reference layout: US Letter (612 x 792 points).
    title = _pdf_sanitize_ascii(_pdf_escape(title))
    body = _pdf_sanitize_ascii(_pdf_escape(body))

    stream = (
        "BT\n"
        "/F1 22 Tf\n"
        "72 740 Td\n"
        f"({title}) Tj\n"
        "/F1 12 Tf\n"
        "0 -28 Td\n"
        f"({body}) Tj\n"
        "ET\n"
    ).encode("ascii", errors="strict")

    objects: list[bytes] = []

    def add_obj(obj_num: int, payload: bytes) -> None:
        objects.append(f"{obj_num} 0 obj\n".encode("ascii") + payload + b"\nendobj\n")

    add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    )
    add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add_obj(5, f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream")

    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    body_bytes = b""
    offsets: list[int] = [0]  # xref entry 0 is the free object

    # Build body and offsets
    cursor = len(header)
    for obj in objects:
        offsets.append(cursor)
        body_bytes += obj
        cursor += len(obj)

    # xref table
    xref_start = len(header) + len(body_bytes)
    xref = [b"xref\n", f"0 {len(offsets)}\n".encode("ascii")]
    xref.append(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n".encode("ascii"))

    trailer = (
        b"trailer\n"
        + f"<< /Size {len(offsets)} /Root 1 0 R >>\n".encode("ascii")
        + b"startxref\n"
        + f"{xref_start}\n".encode("ascii")
        + b"%%EOF\n"
    )

    return header + body_bytes + b"".join(xref) + trailer


def generate_dummy_uploads(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().isoformat(timespec="seconds")

    pdf_specs = [
        PdfSpec(
            filename="intake_form_client_a.pdf",
            title="Dummy Intake Form (Client A)",
            body=f"Generated {timestamp} - sample upload for testing.",
        ),
        PdfSpec(
            filename="policy_summary.pdf",
            title="Dummy Policy Summary",
            body="This is not a real policy; it is placeholder text.",
        ),
        PdfSpec(
            filename="invoice_000123.pdf",
            title="Dummy Invoice #000123",
            body="Amount due: $123.45 (example only).",
        ),
        PdfSpec(
            filename="medical_record_excerpt.pdf",
            title="Dummy Medical Record Excerpt",
            body="Contains no real patient information.",
        ),
        PdfSpec(
            filename="consent_form.pdf",
            title="Dummy Consent Form",
            body="Signature: __________________________",
        ),
    ]

    # Create a sample library folder structure (so you can upload a directory).
    library_dir = out_dir / "library_sample" / "folder_1"
    library_dir.mkdir(parents=True, exist_ok=True)

    created: dict[str, Path] = {}
    for spec in pdf_specs:
        pdf_path = library_dir / spec.filename
        pdf_path.write_bytes(_make_simple_pdf_bytes(spec.title, spec.body))
        created[spec.filename] = pdf_path

    # Create a 2-question XLSX suitable for llm_utility_mock._load_questions().
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "openpyxl is required to generate the dummy questions.xlsx. "
            "Install it with: pip install openpyxl"
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "questions"

    ws.append(["id", "question"])
    ws.append(["q1", "What is the applicant's full name?"])
    ws.append(["q2", "What date is the service being requested for?"])

    questions_path = out_dir / "questions.xlsx"
    wb.save(questions_path)
    created["questions.xlsx"] = questions_path

    return created


def main() -> None:
    out_dir = Path("dummy_uploads")
    created = generate_dummy_uploads(out_dir)

    print("Created dummy upload files:")
    for name, path in created.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
