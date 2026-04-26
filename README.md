# FormBot Streamlit Mock

This is a small Streamlit frontend for the mocked `prepare()` and `process()` workflow.

## Run

```powershell
pip install -r requirements.txt
streamlit run app.py
```

Uploaded libraries, question sets, and run outputs are stored in `app_data/`.

## Mock Behavior

- `prepare(folders)` creates Markdown files beside uploaded PDFs. If a folder has no PDFs, it creates `mock_document.md`.
- `process(folders, questions.xlsx, output_dir, arrangement)` creates `results.jsonl` and `results.xlsx`.
- Each JSONL row contains `arrangement`, `folder`, `id`, `question`, and a random mock `answer`.

When the real utility is ready to wire in, replace the imports in `app.py` with your production `prepare` and `process` functions, or update `llm_utility_mock.py` to call them.
