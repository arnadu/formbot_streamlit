from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st
from streamlit_tree_select import tree_select

from llm_utility_mock import load_template, prepare_file, process_folder, write_results


APP_DATA = Path("app_data")
LIBRARIES_DIR = APP_DATA / "libraries"
QUESTION_SETS_DIR = APP_DATA / "question_sets"
RUNS_DIR = APP_DATA / "runs"
FLASH_MESSAGE_KEY = "_flash_message"
LIBRARY_FILE_SUFFIXES = {".pdf", ".md"}
ACTIVE_LIBRARY_KEY = "active_library"

MAKER_SYSTEM_PROMPT = "You are a document extraction expert. Extract the requested information accurately."
CHECKER_SYSTEM_PROMPT = "You are a quality checker. Verify that the extracted information is correct."
LLM_PARAMS: dict = {}

# Module-level job registry. Keys are job_ids; values hold status, per-item log,
# and final result. Accessed from both the main thread and background threads;
# list.append is GIL-atomic in CPython so no explicit lock is needed.
_jobs: dict[str, dict] = {}


def main() -> None:
    st.set_page_config(page_title="FormBot Utility", layout="wide")
    ensure_storage()
    render_sidebar()
    render_flash_message()
    render_main_page()


def render_flash_message() -> None:
    message = st.session_state.pop(FLASH_MESSAGE_KEY, None)
    if message:
        st.success(message)


def flash(message: str) -> None:
    st.session_state[FLASH_MESSAGE_KEY] = message


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    with st.sidebar:
        st.title("FormBot")
        st.divider()
        render_question_upload_sidebar()
        st.divider()
        render_library_management_sidebar()


def render_library_management_sidebar() -> None:
    libraries = list_named_dirs(LIBRARIES_DIR)

    st.subheader("Library")

    if libraries:
        active_idx = _active_library_index(libraries)
        selected = st.selectbox(
            "Select library",
            options=libraries,
            index=active_idx,
            format_func=lambda p: p.name,
            key="sidebar_library_select",
            label_visibility="collapsed",
        )
        st.session_state[ACTIVE_LIBRARY_KEY] = selected.name

    with st.expander("+ New library", expanded=not libraries):
        new_name = st.text_input(
            "Library name",
            placeholder="Client A – April files",
            key="new_library_name",
            label_visibility="collapsed",
        )
        if st.button(
            "Create library",
            disabled=not new_name.strip(),
            key="create_library_btn",
            type="primary",
        ):
            slug = slugify(new_name)
            path = LIBRARIES_DIR / slug
            if path.exists():
                st.error(f"Already exists: `{slug}`")
            else:
                path.mkdir(parents=True, exist_ok=True)
                st.session_state[ACTIVE_LIBRARY_KEY] = slug
                st.rerun()

    if not libraries:
        return

    active_name = st.session_state.get(ACTIVE_LIBRARY_KEY, libraries[0].name)
    library = LIBRARIES_DIR / active_name
    if not library.exists():
        library = libraries[0]

    st.divider()
    st.subheader("Upload files")

    folders = list_library_folders(library)

    with st.expander("+ New folder", expanded=not folders):
        new_folder = st.text_input(
            "Folder name",
            placeholder="folder-name",
            key=f"sidebar_new_folder_{library.name}",
            label_visibility="collapsed",
        )
        if st.button(
            "Create folder",
            disabled=not new_folder.strip(),
            key=f"sidebar_create_folder_{library.name}",
            type="primary",
        ):
            slug = slugify(new_folder)
            fp = library / slug
            if fp.exists():
                st.error(f"Already exists: `{slug}`")
            else:
                fp.mkdir(parents=True, exist_ok=True)
                st.session_state[f"sidebar_active_folder_{library.name}"] = slug
                flash(f"Created folder `{slug}`.")
                st.rerun()

    if folders:
        active_folder_key = f"sidebar_active_folder_{library.name}"
        active_folder_name = st.session_state.get(active_folder_key, folders[0].name)
        active_folder = library / active_folder_name
        if not active_folder.exists():
            active_folder = folders[0]

        selected_folder = st.selectbox(
            "Upload into folder",
            folders,
            index=folders.index(active_folder) if active_folder in folders else 0,
            format_func=lambda p: p.name,
            key=f"sidebar_folder_select_{library.name}",
        )
        st.session_state[active_folder_key] = selected_folder.name

        folder_upload_key = f"sidebar_files_{library.name}_{selected_folder.name}"
        st.file_uploader(
            f"PDF / Markdown → {selected_folder.name}",
            accept_multiple_files=True,
            type=["pdf", "md"],
            key=folder_upload_key,
            on_change=_on_upload,
            args=(folder_upload_key, selected_folder, False),
        )

    dir_upload_key = f"sidebar_dir_{library.name}"
    st.file_uploader(
        "Upload folder tree",
        accept_multiple_files="directory",
        key=dir_upload_key,
        on_change=_on_upload,
        args=(dir_upload_key, library, True),
    )

    _start_pending_prepare()


@st.fragment
def render_question_upload_sidebar() -> None:
    generation = st.session_state.get("qs_upload_gen", 0)
    st.subheader("Question Sets")

    if msg := st.session_state.pop("qs_saved_message", None):
        st.success(msg)

    st.caption("Named after the uploaded file. Upload again to replace.")
    question_file = st.file_uploader(
        "Upload .xlsx",
        type=["xlsx"],
        key=f"qs_upload_{generation}",
        label_visibility="collapsed",
    )
    if question_file is not None:
        saved_name = save_question_set(question_file)
        st.session_state["qs_upload_gen"] = generation + 1
        st.session_state["qs_saved_message"] = f"Saved `{saved_name}`."
        st.rerun()


def _active_library_index(libraries: list[Path]) -> int:
    active_name = st.session_state.get(ACTIVE_LIBRARY_KEY)
    if active_name:
        for i, lib in enumerate(libraries):
            if lib.name == active_name:
                return i
    return 0


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

def render_main_page() -> None:
    libraries = list_named_dirs(LIBRARIES_DIR)
    question_sets = list_named_dirs(QUESTION_SETS_DIR)

    if not libraries:
        st.info("Create a library in the sidebar to get started.")
        return

    # ── Prepare progress ──────────────────────────────────────────────────────
    # Poll by sleeping and calling st.rerun() from the main script — simpler and
    # more reliable than fragments for this use case.
    active_prepare_id = st.session_state.get("active_prepare_id")
    if active_prepare_id:
        job = _jobs.get(active_prepare_id)
        if job:
            with st.status("Running prepare()…", expanded=True) as status:
                for msg in job["progress"]:
                    st.write(msg)
                if job["status"] == "complete":
                    count = len(job["progress"])
                    status.update(label=f"prepare() complete — {count} file(s) processed.", state="complete")
                    _jobs.pop(active_prepare_id, None)
                    del st.session_state["active_prepare_id"]
                    st.rerun()
                elif job["status"] == "error":
                    status.update(label=f"prepare() failed: {job['error']}", state="error")
                    _jobs.pop(active_prepare_id, None)
                    del st.session_state["active_prepare_id"]
                    st.rerun()
                else:
                    time.sleep(0.5)
                    st.rerun()
        else:
            time.sleep(0.1)
            st.rerun()

    # ── Run progress ──────────────────────────────────────────────────────────
    active_run_id = st.session_state.get("active_run_id")
    if active_run_id:
        job = _jobs.get(active_run_id)
        if job:
            with st.status("Running process()…", expanded=True) as status:
                for msg in job["progress"]:
                    st.write(msg)
                if job["status"] == "complete":
                    run_id = job.get("result", "")
                    count = len(job["progress"])
                    status.update(label=f"process() complete — {count} folder(s) processed.", state="complete")
                    flash(f"Run `{run_id}` complete.")
                    _jobs.pop(active_run_id, None)
                    del st.session_state["active_run_id"]
                    st.rerun()
                elif job["status"] == "error":
                    status.update(label=f"process() failed: {job['error']}", state="error")
                    flash(f"Run failed: {job['error']}")
                    _jobs.pop(active_run_id, None)
                    del st.session_state["active_run_id"]
                    st.rerun()
                else:
                    time.sleep(0.5)
                    st.rerun()
        else:
            time.sleep(0.1)
            st.rerun()

    # ── Controls row ─────────────────────────────────────────────────────────
    lib_col, qs_col, btn_col = st.columns([4, 4, 2], vertical_alignment="bottom")

    with lib_col:
        st.caption("Library")
        selected_lib = st.selectbox(
            "Library",
            libraries,
            index=_active_library_index(libraries),
            format_func=lambda p: p.name,
            key="main_library_select",
            label_visibility="collapsed",
        )
        st.session_state[ACTIVE_LIBRARY_KEY] = selected_lib.name

    library = selected_lib
    folders = list_library_folders(library)
    nodes, default_checked = build_tree_nodes(folders)

    with qs_col:
        st.caption("Question set")
        if question_sets:
            selected_qs = st.selectbox(
                "Question set",
                question_sets,
                format_func=lambda p: p.name,
                key="main_qs_select",
                label_visibility="collapsed",
            )
        else:
            st.caption("Upload a question set in the sidebar.")
            selected_qs = None

    # ── File tree ─────────────────────────────────────────────────────────────
    # btn_col is intentionally filled AFTER the tree so the Run button can read
    # result["checked"] directly — columns are containers whose visual position
    # is fixed at creation; writing to one later does not move it on the page.
    st.divider()

    if not folders:
        st.info("No folders yet — create one in the sidebar and upload files.")
        checked: set[str] = set()
    else:
        # Key includes a content hash so the tree re-initialises (and picks up the
        # correct checked list) whenever files are added or removed.
        stems_sig = hashlib.md5(
            ",".join(sorted(c["value"] for n in nodes for c in n.get("children", []))).encode()
        ).hexdigest()[:8]
        result = tree_select(
            nodes,
            checked=default_checked,
            check_model="leaf",
            expand_on_click=True,
            show_expand_all=True,
            key=f"file_tree_{library.name}_{stems_sig}",
        )
        checked = set(result["checked"])

    with btn_col:
        job_running = bool(
            st.session_state.get("active_prepare_id") or st.session_state.get("active_run_id")
        )
        can_run = bool(checked) and selected_qs is not None and not job_running
        if st.button(
            "▶  Run",
            type="primary",
            disabled=not can_run,
            key="run_btn",
            use_container_width=True,
        ):
            _do_run(library, selected_qs, checked)

    if selected_qs:
        questions_xlsx = selected_qs / "questions.xlsx"
        if questions_xlsx.exists():
            with st.expander("Preview questions"):
                st.dataframe(pd.read_excel(questions_xlsx), width="stretch")

    st.divider()
    render_results_panel(library.name, selected_qs.name if selected_qs else None)


def build_tree_nodes(folders: list[Path]) -> tuple[list[dict], list[str]]:
    # All stems checked by default. The node value "folder/stem" is the key used
    # to match tree selections back to files in materialize_included_inputs.
    nodes: list[dict] = []
    checked: list[str] = []
    for folder in folders:
        stems = sorted({f.stem for f in list_scope_files(folder)})
        children = []
        for stem in stems:
            value = f"{folder.name}/{stem}"
            children.append({"label": stem, "value": value})
            checked.append(value)
        nodes.append({"label": folder.name, "value": f"_folder_{folder.name}", "children": children})
    return nodes, checked


def _do_run(library: Path, selected_qs: Path, checked: set[str]) -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    run_path = RUNS_DIR / run_id
    run_path.mkdir(parents=True, exist_ok=True)
    job_id = f"run-{run_id}"
    # Pre-register before starting thread so the polling loop in render_main_page
    # doesn't race against the thread initialising _jobs[job_id].
    _jobs[job_id] = {"status": "running", "progress": [], "error": None, "result": None}
    st.session_state["active_run_id"] = job_id
    threading.Thread(
        target=_background_run,
        args=(job_id, library, selected_qs, checked, run_path),
        daemon=True,
    ).start()
    st.rerun()


def _background_run(
    job_id: str,
    library: Path,
    selected_qs: Path,
    checked: set[str],
    run_path: Path,
) -> None:
    try:
        run_id = run_path.name
        targets = materialize_included_inputs(library, checked, run_path / "_selected_inputs")
        template_df = load_template(selected_qs / "questions.xlsx")
        jsonl_path = run_path / "results.jsonl"

        for folder in targets:
            _jobs[job_id]["progress"].append(f"↳ {folder.name}")
            process_folder(
                MAKER_SYSTEM_PROMPT,
                CHECKER_SYSTEM_PROMPT,
                template_df,
                folder,
                LLM_PARAMS,
                str(jsonl_path),
            )

        xlsx_path = write_results(jsonl_path)
        metadata = {
            "run_id": run_id,
            "library": library.name,
            "question_set": selected_qs.name,
            "jsonl_path": str(jsonl_path),
            "xlsx_path": str(xlsx_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        (run_path / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        _jobs[job_id]["result"] = run_id
        _jobs[job_id]["status"] = "complete"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)


def _run_matches(run_path: Path, library_name: str, qs_name: str | None) -> bool:
    metadata_path = run_path / "metadata.json"
    if not metadata_path.exists():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if metadata.get("library") != library_name:
        return False
    if qs_name and metadata.get("question_set") != qs_name:
        return False
    return True


def render_results_panel(library_name: str, qs_name: str | None) -> None:
    all_runs = list_named_dirs(RUNS_DIR)
    runs = [r for r in all_runs if _run_matches(r, library_name, qs_name)]
    if not runs:
        return

    st.subheader("Results")

    if len(runs) > 1:
        selected_run = st.selectbox(
            "Run",
            runs,
            index=len(runs) - 1,
            format_func=run_label,
            key="results_run_select",
        )
    else:
        selected_run = runs[0]
        st.caption(f"Run: {run_label(selected_run)}")

    jsonl_path = selected_run / "results.jsonl"
    xlsx_path = selected_run / "results.xlsx"

    if jsonl_path.exists():
        rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
        st.dataframe(pd.DataFrame(rows), width="stretch")

    if xlsx_path.exists():
        st.download_button(
            "⬇  Download results.xlsx",
            data=xlsx_path.read_bytes(),
            file_name=f"{selected_run.name}-results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl_{selected_run.name}",
        )


# ---------------------------------------------------------------------------
# materialize inputs for a run
# ---------------------------------------------------------------------------

def materialize_included_inputs(library: Path, checked: set[str], destination_root: Path) -> list[Path]:
    destination_root.mkdir(parents=True, exist_ok=True)

    materialized_targets: list[Path] = []
    for folder in list_library_folders(library):
        included_files = [
            f for f in list_scope_files(folder) if f"{folder.name}/{f.stem}" in checked
        ]
        if not included_files:
            continue

        destination_scope = destination_root / folder.name
        destination_scope.mkdir(parents=True, exist_ok=True)
        for file_path in included_files:
            shutil.copy2(file_path, destination_scope / file_path.name)
        materialized_targets.append(destination_scope)

    return materialized_targets


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _on_upload(upload_key: str, destination: Path, strip_common_root: bool) -> None:
    files = st.session_state.get(upload_key)
    if files:
        saved = save_uploaded_files(files, destination, strip_common_root=strip_common_root)
        flash(f"Uploaded {len(files)} file(s) into `{destination.name}`.")
        # on_change callbacks run before the script rerenders, so st.status/st.write
        # are not available here. We store the saved paths and defer prepare() to
        # _start_pending_prepare(), which is called from the normal render pass.
        pending: set[Path] = st.session_state.get("pending_prepare", set())
        pending |= saved
        st.session_state["pending_prepare"] = pending


def _start_pending_prepare() -> None:
    pending: set[Path] | None = st.session_state.pop("pending_prepare", None)
    if not pending:
        return
    job_id = f"prepare-{uuid4().hex[:8]}"
    _jobs[job_id] = {"status": "running", "progress": [], "error": None}
    st.session_state["active_prepare_id"] = job_id
    threading.Thread(target=_background_prepare, args=(job_id, pending), daemon=True).start()
    st.rerun()


def _background_prepare(job_id: str, pending: set[Path]) -> None:
    try:
        for path in sorted(pending):
            prepare_file(path)
            _jobs[job_id]["progress"].append(f"↳ {path.name}")
        _jobs[job_id]["status"] = "complete"
    except Exception as exc:
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"] = str(exc)


def save_question_set(question_file) -> str:
    target_name = slugify(Path(question_file.name).stem) or "questions"
    question_path = QUESTION_SETS_DIR / target_name
    question_path.mkdir(parents=True, exist_ok=True)
    (question_path / "questions.xlsx").write_bytes(question_file.getbuffer())
    return question_path.name


def save_uploaded_files(
    uploaded_files: list,
    destination: Path,
    strip_common_root: bool = False,
) -> set[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    # For directory uploads the browser prefixes every filename with the dragged
    # folder name (e.g. "acme-docs/contracts/q1.pdf"). Strip that redundant top
    # level so files land directly under `destination` rather than one level deeper.
    common_root = common_upload_root(uploaded_files) if strip_common_root else None

    saved: set[Path] = set()
    for uploaded_file in uploaded_files:
        relative_name = normalize_uploaded_name(uploaded_file.name, common_root=common_root)
        target = destination / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(uploaded_file.getbuffer())
        saved.add(target)
    return saved


def normalize_uploaded_name(name: str, common_root: str | None = None) -> Path:
    # Browser path separators can be / or \ depending on OS; slugify each component.
    parts = [slugify(part) for part in re.split(r"[\\/]+", name) if part not in {"", ".", ".."}]
    if common_root and len(parts) > 1 and parts[0] == common_root:
        parts = parts[1:]
    if not parts:
        return Path(f"uploaded-{uuid4().hex}")
    return Path(*parts)


def common_upload_root(uploaded_files: list) -> str | None:
    # Determines whether a single redundant top-level folder name should be stripped
    # from all uploaded paths before saving. Three cases:
    #
    #   Flat folder — user drags "acme/" containing files directly:
    #     browser sends: acme/file1.pdf, acme/file2.pdf  (2 parts)
    #     → return None (no stripping) → saved as library/acme/file1.pdf ✓
    #
    #   Nested folder — user drags "acme/" containing sub-folders:
    #     browser sends: acme/contracts/q1.pdf, acme/reports/r1.pdf  (3 parts)
    #     → return "acme" → saved as library/contracts/q1.pdf ✓
    #
    #   Multiple folders — user drags "A/" and "B/" together:
    #     browser sends: A/file1.pdf, B/file2.pdf  (different first parts)
    #     → return None (no stripping) → saved as library/A/file1.pdf, library/B/file2.pdf ✓
    #
    # The len < 3 guard handles the flat-folder case: files sitting directly inside
    # the dragged root produce only 2 parts, so we preserve the folder name.
    first_parts: list[str] = []
    for uploaded_file in uploaded_files:
        parts = [slugify(part) for part in re.split(r"[\\/]+", uploaded_file.name) if part not in {"", ".", ".."}]
        if len(parts) < 3:
            return None
        first_parts.append(parts[0])

    if not first_parts:
        return None
    first_root = first_parts[0]
    if all(part == first_root for part in first_parts[1:]):
        return first_root
    return None


# ---------------------------------------------------------------------------
# Library / directory helpers
# ---------------------------------------------------------------------------

def list_library_folders(library: Path) -> list[Path]:
    return sorted(
        path
        for path in library.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def list_scope_files(scope: Path) -> list[Path]:
    return sorted(
        path
        for path in scope.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in LIBRARY_FILE_SUFFIXES
    )


def list_named_dirs(parent: Path) -> list[Path]:
    if not parent.exists():
        return []
    return sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.name)


def run_label(path: Path) -> str:
    metadata_path = path / "metadata.json"
    if not metadata_path.exists():
        return path.name
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return f"{metadata.get('created_at', path.name)} | {metadata.get('library', '')} × {metadata.get('question_set', '')}"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value


def ensure_storage() -> None:
    for path in (APP_DATA, LIBRARIES_DIR, QUESTION_SETS_DIR, RUNS_DIR):
        path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
