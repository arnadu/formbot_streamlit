from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st

from llm_utility_mock import prepare, process


APP_DATA = Path("app_data")
LIBRARIES_DIR = APP_DATA / "libraries"
QUESTION_SETS_DIR = APP_DATA / "question_sets"
RUNS_DIR = APP_DATA / "runs"
LIBRARY_STATE_FILENAME = ".formbot_state.json"
LIBRARY_ROOT_KEY = "__root__"
FLASH_MESSAGE_KEY = "_flash_message"
LIBRARY_FILE_SUFFIXES = {".pdf", ".md"}
ACTIVE_LIBRARY_KEY = "active_library"


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
        render_library_management_sidebar()
        st.divider()
        render_question_upload_sidebar()


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

    # New folder row
    col_name, col_btn = st.columns([3, 1])
    with col_name:
        new_folder = st.text_input(
            "New folder name",
            placeholder="folder-name",
            key=f"sidebar_new_folder_{library.name}",
            label_visibility="collapsed",
        )
    with col_btn:
        if st.button(
            "+ Folder",
            key=f"sidebar_create_folder_{library.name}",
            disabled=not new_folder.strip(),
        ):
            slug = slugify(new_folder)
            fp = library / slug
            if not fp.exists():
                fp.mkdir(parents=True, exist_ok=True)
                st.session_state[f"sidebar_active_folder_{library.name}"] = slug
                flash(f"Created folder `{slug}`.")
                st.rerun()

    folders = list_library_folders(library)

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

        files_upload = st.file_uploader(
            f"PDF / Markdown → {selected_folder.name}",
            accept_multiple_files=True,
            type=["pdf", "md"],
            key=f"sidebar_files_{library.name}_{selected_folder.name}",
        )
        maybe_auto_save_folder_upload(files_upload, selected_folder, library)

    dir_upload = st.file_uploader(
        "Upload folder tree",
        accept_multiple_files="directory",
        key=f"sidebar_dir_{library.name}",
    )
    maybe_auto_save_library_upload(dir_upload, library, f"sidebar_dir::{library.name}")

    if folders:
        if st.button("Run prepare()", key=f"sidebar_prepare_{library.name}"):
            created = prepare(list_library_folders(library))
            flash(f"prepare() created {len(created)} file(s).")
            st.rerun()


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

    active_name = st.session_state.get(ACTIVE_LIBRARY_KEY, libraries[0].name)
    library = LIBRARIES_DIR / active_name
    if not library.exists():
        library = libraries[0]

    state = load_library_state(library)
    folders = list_library_folders(library)
    summary = summarize_library(library, state)

    col_title, col_m1, col_m2, col_m3 = st.columns([4, 1, 1, 1])
    with col_title:
        st.header(library.name)
    with col_m1:
        st.metric("Folders", f"{summary['included_folder_count']}/{summary['folder_count']}")
    with col_m2:
        st.metric("Files included", f"{summary['included_file_count']}/{summary['file_count']}")
    with col_m3:
        st.metric("Excluded", summary["file_count"] - summary["included_file_count"])

    st.divider()

    tree_col, run_col = st.columns([11, 9])

    with tree_col:
        render_file_tree(library, folders, state)

    with run_col:
        render_run_panel(library, question_sets, state, summary)

    st.divider()
    render_results_panel()


def render_file_tree(library: Path, folders: list[Path], state: dict) -> None:
    st.subheader("Files")

    if not folders:
        st.info("No folders yet — create one in the sidebar and upload files.")
        return

    st.caption("Check or uncheck folders and files to include them in processing.")

    for folder in folders:
        files = list_scope_files(folder)
        included_count = sum(1 for f in files if is_file_included(library, f, state))
        folder_included = is_scope_included(library, folder, state)

        new_folder_included = st.checkbox(
            f"📁 **{folder.name}** — {included_count} / {len(files)} included",
            value=folder_included,
            key=f"tree_folder_{library.name}_{folder.name}",
        )
        if new_folder_included != folder_included:
            set_scope_included(library, folder, state, new_folder_included)
            save_library_state(library, state)
            st.rerun()

        for file_path in files:
            _, fcol = st.columns([1, 20])
            with fcol:
                icon = "📄" if file_path.suffix.lower() == ".md" else "📋"
                cur = is_file_included(library, file_path, state)
                new = st.checkbox(
                    f"{icon} {file_path.name}",
                    value=cur,
                    key=f"tree_file_{library.name}_{file_key(file_path, library)}",
                )
                if new != cur:
                    set_file_included(library, file_path, state, new)
                    save_library_state(library, state)


def render_run_panel(
    library: Path,
    question_sets: list[Path],
    state: dict,
    summary: dict,
) -> None:
    st.subheader("Run")

    if not question_sets:
        st.info("Upload a question set in the sidebar to enable running.")
        return

    selected_qs = st.selectbox(
        "Question set",
        question_sets,
        format_func=lambda p: p.name,
        key="run_panel_qs",
    )

    questions_xlsx = selected_qs / "questions.xlsx"
    if questions_xlsx.exists():
        with st.expander("Preview questions"):
            st.dataframe(pd.read_excel(questions_xlsx), width="stretch")

    can_run = summary["included_file_count"] > 0
    if not can_run:
        st.warning("No files included. Check at least one file in the tree.")

    if st.button(
        "▶  Run process()",
        type="primary",
        disabled=not can_run,
        key="run_process_btn",
        width="stretch",
    ):
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        run_path = RUNS_DIR / run_id
        targets = materialize_included_inputs(library, state, run_path / "_selected_inputs")
        jsonl_path, xlsx_path = process(targets, questions_xlsx, run_path, library.name)
        metadata = {
            "run_id": run_id,
            "library": library.name,
            "question_set": selected_qs.name,
            "included_folder_count": summary["included_folder_count"],
            "included_file_count": summary["included_file_count"],
            "jsonl_path": str(jsonl_path),
            "xlsx_path": str(xlsx_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        (run_path / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        flash(f"Run `{run_id}` complete.")
        st.rerun()


def render_results_panel() -> None:
    runs = list_named_dirs(RUNS_DIR)
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

def materialize_included_inputs(library: Path, state: dict, destination_root: Path) -> list[Path]:
    destination_root.mkdir(parents=True, exist_ok=True)

    materialized_targets: list[Path] = []
    for folder in list_library_folders(library):
        included_files = [
            f for f in list_scope_files(folder) if is_file_included(library, f, state)
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

def maybe_auto_save_library_upload(
    uploaded_files: list | None,
    destination: Path,
    upload_key: str,
) -> None:
    if not uploaded_files:
        return

    signature = upload_signature(uploaded_files)
    processed_key = f"processed_upload::{upload_key}"
    if st.session_state.get(processed_key) == signature:
        return

    save_uploaded_files(uploaded_files, destination, strip_common_root=True)
    st.session_state[processed_key] = signature
    flash(f"Uploaded {len(uploaded_files)} file(s) into `{destination.name}`.")
    st.rerun()


def maybe_auto_save_folder_upload(
    uploaded_files: list | None,
    folder: Path,
    library: Path,
) -> None:
    if not uploaded_files:
        return

    signature = upload_signature(uploaded_files)
    processed_key = f"processed_upload::folder::{library.name}::{scope_key(folder, library)}"
    if st.session_state.get(processed_key) == signature:
        return

    save_uploaded_files(uploaded_files, folder)
    st.session_state[processed_key] = signature
    flash(f"Uploaded {len(uploaded_files)} file(s) into `{folder.name}`.")
    st.rerun()


def save_question_set(question_file) -> str:
    target_name = slugify(Path(question_file.name).stem) or "questions"
    question_path = QUESTION_SETS_DIR / target_name
    question_path.mkdir(parents=True, exist_ok=True)
    (question_path / "questions.xlsx").write_bytes(question_file.getbuffer())
    return question_path.name


def upload_signature(uploaded_files: list) -> tuple[tuple[str, int], ...]:
    return tuple((f.name, len(f.getbuffer())) for f in uploaded_files)


def save_uploaded_files(
    uploaded_files: list,
    destination: Path,
    strip_common_root: bool = False,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    common_root = common_upload_root(uploaded_files) if strip_common_root else None

    for uploaded_file in uploaded_files:
        relative_name = normalize_uploaded_name(uploaded_file.name, common_root=common_root)
        target = destination / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(uploaded_file.getbuffer())


def normalize_uploaded_name(name: str, common_root: str | None = None) -> Path:
    parts = [slugify(part) for part in re.split(r"[\\/]+", name) if part not in {"", ".", ".."}]
    if common_root and len(parts) > 1 and parts[0] == common_root:
        parts = parts[1:]
    if not parts:
        return Path(f"uploaded-{uuid4().hex}")
    return Path(*parts)


def common_upload_root(uploaded_files: list) -> str | None:
    first_parts: list[str] = []
    for uploaded_file in uploaded_files:
        parts = [slugify(part) for part in re.split(r"[\\/]+", uploaded_file.name) if part not in {"", ".", ".."}]
        if len(parts) < 2:
            return None
        first_parts.append(parts[0])

    if not first_parts:
        return None
    first_root = first_parts[0]
    if all(part == first_root for part in first_parts[1:]):
        return first_root
    return None


# ---------------------------------------------------------------------------
# State: include/exclude (persisted to disk)
# ---------------------------------------------------------------------------

def load_library_state(library: Path) -> dict:
    state_path = library / LIBRARY_STATE_FILENAME
    if not state_path.exists():
        return {"folders": {}, "files": {}}

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"folders": {}, "files": {}}

    return {
        "folders": data.get("folders", {}),
        "files": data.get("files", {}),
    }


def save_library_state(library: Path, state: dict) -> None:
    (library / LIBRARY_STATE_FILENAME).write_text(json.dumps(state, indent=2), encoding="utf-8")


def scope_key(scope: Path, library: Path) -> str:
    if scope == library:
        return LIBRARY_ROOT_KEY
    return str(scope.relative_to(library)).replace("\\", "/")


def file_key(path: Path, library: Path) -> str:
    return str(path.relative_to(library)).replace("\\", "/")


def is_scope_included(library: Path, scope: Path, state: dict) -> bool:
    return bool(state.get("folders", {}).get(scope_key(scope, library), {}).get("included", True))


def set_scope_included(library: Path, scope: Path, state: dict, included: bool) -> None:
    folders = state.setdefault("folders", {})
    files = state.setdefault("files", {})
    folders[scope_key(scope, library)] = {"included": included}
    for file_path in list_scope_files(scope):
        files[file_key(file_path, library)] = {"included": included}


def is_file_included(library: Path, path: Path, state: dict) -> bool:
    file_state = state.get("files", {}).get(file_key(path, library), {})
    if "included" in file_state:
        return bool(file_state["included"])
    return is_scope_included(library, path.parent, state)


def set_file_included(library: Path, path: Path, state: dict, included: bool) -> None:
    state.setdefault("files", {})[file_key(path, library)] = {"included": included}


# ---------------------------------------------------------------------------
# Library / directory helpers
# ---------------------------------------------------------------------------

def summarize_library(library: Path, state: dict) -> dict[str, int]:
    folders = list_library_folders(library)
    file_count = 0
    included_file_count = 0
    included_folder_count = 0

    for folder in folders:
        files = list_scope_files(folder)
        file_count += len(files)
        included_files = [f for f in files if is_file_included(library, f, state)]
        included_file_count += len(included_files)
        if included_files:
            included_folder_count += 1

    return {
        "folder_count": len(folders),
        "file_count": file_count,
        "included_file_count": included_file_count,
        "included_folder_count": included_folder_count,
    }


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
        and path.name != LIBRARY_STATE_FILENAME
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
