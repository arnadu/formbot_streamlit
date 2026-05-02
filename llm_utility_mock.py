from __future__ import annotations

import json
import random
import string
from pathlib import Path
from typing import Iterable

import pandas as pd


def prepare_file(pdf_path: str | Path) -> Path | None:
    """Mock OCR step: create a Markdown file for a single PDF. Returns the created file."""
    pdf_path = Path(pdf_path)
    if pdf_path.suffix.lower() != ".pdf":
        return None
    md_file = pdf_path.with_suffix(".md")
    md_file.write_text(
        f"# OCR output for {pdf_path.name}\n\n"
        "This is mocked Markdown content that stands in for OCR output.\n",
        encoding="utf-8",
    )
    return md_file


def prepare(folder_names: Iterable[str | Path]) -> list[Path]:
    """Mock OCR step: create one Markdown file for each PDF found in each folder."""
    created_files: list[Path] = []

    for folder_name in folder_names:
        folder = Path(folder_name)
        folder.mkdir(parents=True, exist_ok=True)

        pdf_files = sorted(folder.glob("*.pdf"))
        if not pdf_files:
            mock_md = folder / "mock_document.md"
            mock_md.write_text(
                "# Mock document\n\n"
                f"No PDF files were uploaded to `{folder.name}`, so prepare() "
                "created this placeholder Markdown file.\n",
                encoding="utf-8",
            )
            created_files.append(mock_md)
            continue

        for pdf_file in pdf_files:
            md_file = prepare_file(pdf_file)
            if md_file:
                created_files.append(md_file)

    return created_files


def process_folder(
    maker_system_prompt: str,
    checker_system_prompt: str,
    template_df: pd.DataFrame,
    arrangement: Path,
    llm_params: dict,
    output_path: str,
) -> None:
    """Mock LLM step for one folder: append answer rows to the JSONL output file.

    In the real implementation maker_system_prompt, checker_system_prompt and
    llm_params drive the LLM calls; template_df provides the question rows;
    arrangement is the folder of prepared .md files; output_path is the run's
    JSONL file, opened in append mode so concurrent/sequential calls accumulate.
    """
    arrangement = Path(arrangement)
    md_files = sorted(arrangement.glob("*.md"))
    source_names = ", ".join(f.name for f in md_files) or "no md files"

    with open(output_path, "a", encoding="utf-8") as fh:
        for _, row in template_df.iterrows():
            result = {
                "arrangement": arrangement.name,
                "question": str(row.iloc[-1]),
                "answer": _random_answer(source_names),
            }
            fh.write(json.dumps(result) + "\n")


def write_results(jsonl_path: str | Path) -> Path:
    """Convert the JSONL results file to XLSX in the same directory."""
    jsonl_path = Path(jsonl_path)
    xlsx_path = jsonl_path.with_name("results.xlsx")
    rows = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pd.DataFrame(rows).to_excel(xlsx_path, index=False)
    return xlsx_path


def load_template(questions_xlsx_path: str | Path) -> pd.DataFrame:
    """Load the questions template from an xlsx file."""
    return pd.read_excel(questions_xlsx_path)


def _random_answer(source_names: str) -> str:
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"Mock answer {token} generated from {source_names}."
