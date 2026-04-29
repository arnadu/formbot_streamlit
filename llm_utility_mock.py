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
    folder: str | Path,
    questions: list[dict[str, str]],
    arrangement: str = "default",
) -> list[dict[str, str]]:
    """Mock LLM step for one folder: return answer rows for all questions."""
    folder = Path(folder)
    md_files = sorted(folder.glob("*.md"))
    source_names = ", ".join(md_file.name for md_file in md_files) or "no md files"
    return [
        {
            "arrangement": arrangement,
            "folder": folder.name,
            "id": q["id"],
            "question": q["question"],
            "answer": _random_answer(source_names),
        }
        for q in questions
    ]


def write_results(rows: list[dict], output_path: str | Path) -> tuple[Path, Path]:
    """Write accumulated answer rows to JSONL and XLSX files."""
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_path / "results.jsonl"
    xlsx_path = output_path / "results.xlsx"
    jsonl_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    pd.DataFrame(rows).to_excel(xlsx_path, index=False)
    return jsonl_path, xlsx_path


def process(
    folder_names: Iterable[str | Path],
    questions_xlsx_path: str | Path,
    output_dir: str | Path,
    arrangement: str = "default",
) -> tuple[Path, Path]:
    """Mock LLM step: delegates to process_folder + write_results."""
    questions = load_questions(questions_xlsx_path)
    rows: list[dict[str, str]] = []
    for folder_name in folder_names:
        rows.extend(process_folder(folder_name, questions, arrangement))
    return write_results(rows, output_dir)


def load_questions(questions_xlsx_path: str | Path) -> list[dict[str, str]]:
    dataframe = pd.read_excel(questions_xlsx_path)
    dataframe.columns = [str(column).strip().lower() for column in dataframe.columns]

    if "id" not in dataframe.columns:
        dataframe.insert(0, "id", [f"q{i + 1}" for i in range(len(dataframe))])

    question_column = "question" if "question" in dataframe.columns else dataframe.columns[-1]

    questions: list[dict[str, str]] = []
    for _, row in dataframe.iterrows():
        if pd.isna(row[question_column]):
            continue
        questions.append(
            {
                "id": str(row["id"]),
                "question": str(row[question_column]),
            }
        )

    return questions


def _random_answer(source_names: str) -> str:
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"Mock answer {token} generated from {source_names}."
