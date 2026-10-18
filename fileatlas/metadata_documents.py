"""Read-only document classification and archive inspection helpers from v6.

This module never moves, extracts, overwrites, or deletes user files.
"""
import os
import zipfile
from datetime import datetime
from pathlib import Path
BUFFER_SIZE = 16 * 1024 * 1024
MIN_FREE_SPACE = 500 * 1024 * 1024
CLASSIFY_DIR_NAMES = {'picture_classify', 'video_classify', 'audio_classify', 'document_classify'}
SKIP_DIRECTORY_NAMES = {'$recycle.bin', 'system volume information', 'media_classify_logs', 'document_classify_logs', 'all_classify_logs', 'organizer_logs'}
ZIP_JUNK_NAMES = {'.ds_store', 'thumbs.db', 'desktop.ini'}
DOCUMENT_CATEGORIES = {'PDF': {'.pdf', '.xps', '.oxps'}, 'Word': {'.doc', '.docx', '.docm', '.dot', '.dotx', '.dotm', '.odt', '.ott', '.rtf'}, 'Spreadsheet': {'.xls', '.xlsx', '.xlsm', '.xlsb', '.xlt', '.xltx', '.xltm', '.ods', '.ots'}, 'Presentation': {'.ppt', '.pptx', '.pptm', '.pps', '.ppsx', '.ppsm', '.pot', '.potx', '.potm', '.odp', '.otp'}, 'Text': {'.txt', '.text', '.nfo'}, 'Markdown': {'.md', '.markdown', '.mdown', '.mkd', '.rst', '.adoc', '.asciidoc'}, 'Ebook': {'.epub', '.mobi', '.azw', '.azw3', '.fb2', '.djvu', '.djv'}, 'LaTeX': {'.tex', '.bib', '.bst', '.cls', '.sty'}, 'Notes_MindMap': {'.one', '.onetoc2', '.xmind', '.mm', '.mmap', '.opml'}, 'Diagram': {'.vsd', '.vsdx', '.vss', '.vssx', '.vst', '.vstx', '.drawio'}, 'WebArchive': {'.mht', '.mhtml', '.chm'}, 'Apple_iWork': {'.pages', '.numbers', '.key'}, 'WPS_Office': {'.wps', '.wpt', '.et', '.ett', '.dps', '.dpt'}, 'Data_Table': {'.csv', '.tsv'}, 'Other_Document': {'.wpd', '.abw', '.sxw', '.sxc', '.sxi'}}
STRUCTURED_DATA_EXTENSIONS = {'.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf'}
PROJECT_SENSITIVE_EXTENSIONS = {'.txt', '.text', '.nfo', '.md', '.markdown', '.mdown', '.mkd', '.rst', '.adoc', '.asciidoc', '.csv', '.tsv', '.tex', '.bib', '.bst', '.cls', '.sty', '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf'}
PROJECT_MARKER_FILENAMES = {'.git', '.vscode', '.idea', 'pyproject.toml', 'requirements.txt', 'setup.py', 'setup.cfg', 'pipfile', 'cargo.toml', 'cargo.lock', 'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'cmakelists.txt', 'makefile', 'pom.xml', 'build.gradle', 'settings.gradle', 'global.json'}
PROJECT_MARKER_EXTENSIONS = {'.sln', '.vcxproj', '.csproj', '.fsproj', '.code-workspace'}
PROJECT_PATH_PARTS = {'.git', '.svn', '.hg', 'node_modules', 'venv', '.venv', '__pycache__', 'target', 'build', 'dist', '.idea', '.vscode'}
PROJECT_ARCHIVE_EXTENSIONS = {'.py', '.pyc', '.ipynb', '.js', '.ts', '.tsx', '.jsx', '.html', '.htm', '.css', '.scss', '.less', '.c', '.cc', '.cpp', '.h', '.hpp', '.rs', '.go', '.java', '.cs', '.sln', '.vcxproj', '.csproj', '.gradle', '.blend', '.fbx', '.obj', '.gltf', '.glb', '.prproj', '.aep', '.unity', '.unitypackage', '.uproject', '.kicad_pcb', '.kicad_sch', '.brd', '.sch', '.mph', '.wbpj', '.cae', '.odb'}
PROJECT_ARCHIVE_FILENAMES = {'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'cargo.toml', 'cargo.lock', 'cmakelists.txt', 'makefile', 'requirements.txt', 'pyproject.toml', 'dockerfile'}

def build_extension_map(include_structured_data=False):
    result = {}
    for category, extensions in DOCUMENT_CATEGORIES.items():
        for ext in extensions:
            result[ext.lower()] = category
    if include_structured_data:
        for ext in STRUCTURED_DATA_EXTENSIONS:
            result[ext.lower()] = 'Structured_Data'
    return result

def get_creation_timestamp(path: Path) -> float:
    return os.path.getctime(path)
_project_cache = {}

def directory_looks_like_project(directory: Path) -> bool:
    key = str(directory).lower()
    if key in _project_cache:
        return _project_cache[key]
    try:
        names = {entry.name.lower() for entry in os.scandir(directory)}
    except Exception:
        _project_cache[key] = False
        return False
    if names & PROJECT_MARKER_FILENAMES:
        _project_cache[key] = True
        return True
    for name in names:
        if Path(name).suffix.lower() in PROJECT_MARKER_EXTENSIONS:
            _project_cache[key] = True
            return True
    _project_cache[key] = False
    return False

def path_is_project_sensitive(path: Path, source_root: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    if parts & PROJECT_PATH_PARTS:
        return True
    current = path.parent
    for _ in range(4):
        if directory_looks_like_project(current):
            return True
        if current == source_root:
            break
        if current.parent == current:
            break
        current = current.parent
    return False

def zip_member_is_junk(info: zipfile.ZipInfo) -> bool:
    name = info.filename.replace('\\', '/')
    parts = [p for p in name.split('/') if p]
    if not parts:
        return True
    if '__MACOSX' in parts:
        return True
    return parts[-1].lower() in ZIP_JUNK_NAMES

def archive_project_score(name: str) -> int:
    normalized = name.replace('\\', '/')
    parts = [p.lower() for p in normalized.split('/') if p]
    basename = parts[-1] if parts else ''
    ext = Path(basename).suffix.lower()
    score = 0
    if ext in PROJECT_ARCHIVE_EXTENSIONS:
        score += 2
    if basename in PROJECT_ARCHIVE_FILENAMES:
        score += 3
    if any((p in PROJECT_PATH_PARTS for p in parts[:-1])):
        score += 1
    return score

def analyze_zip(zip_path: Path, extension_map: dict, min_docs: int, min_doc_ratio: float, min_byte_ratio: float):
    result = {'status': 'NOT_DOCUMENT_ARCHIVE', 'reason': '', 'total_files': 0, 'document_count': 0, 'document_ratio': 0.0, 'document_bytes_ratio': 0.0, 'project_score': 0, 'pure_documents': False, 'encrypted': False, 'uncompressed_total': 0, 'uncompressed_documents': 0, 'document_infos': []}
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            infos = [i for i in zf.infolist() if not i.is_dir() and (not zip_member_is_junk(i))]
    except Exception as e:
        result['status'] = 'ZIP_ERROR'
        result['reason'] = str(e)
        return result
    if not infos:
        result['reason'] = 'No meaningful files'
        return result
    total_bytes = 0
    doc_bytes = 0
    doc_infos = []
    project_score = 0
    encrypted = False
    for info in infos:
        total_bytes += max(0, info.file_size)
        encrypted = encrypted or bool(info.flag_bits & 1)
        project_score += archive_project_score(info.filename)
        category = extension_map.get(Path(info.filename).suffix.lower())
        if category:
            doc_infos.append((info, category))
            doc_bytes += max(0, info.file_size)
    total = len(infos)
    count = len(doc_infos)
    ratio = count / total if total else 0.0
    byte_ratio = doc_bytes / total_bytes if total_bytes else 0.0
    pure = count == total and total > 0
    result.update({'total_files': total, 'document_count': count, 'document_ratio': ratio, 'document_bytes_ratio': byte_ratio, 'project_score': project_score, 'pure_documents': pure, 'encrypted': encrypted, 'uncompressed_total': total_bytes, 'uncompressed_documents': doc_bytes, 'document_infos': doc_infos})
    if encrypted:
        result['status'] = 'ENCRYPTED'
        result['reason'] = 'Archive contains encrypted members'
        return result
    if count == 0:
        result['reason'] = 'No recognized documents'
        return result
    if pure and count <= 4:
        result['status'] = 'DOCUMENT_ARCHIVE'
        result['reason'] = 'Small pure document archive'
        return result
    if count >= min_docs and ratio >= min_doc_ratio and (byte_ratio >= min_byte_ratio) and (not (project_score > 0 and (not pure))):
        result['status'] = 'DOCUMENT_ARCHIVE'
        result['reason'] = f'document_ratio={ratio:.1%}, byte_ratio={byte_ratio:.1%}'
        return result
    if ratio >= 0.35 or count >= 10:
        result['status'] = 'REVIEW'
        result['reason'] = f'Borderline archive: document_ratio={ratio:.1%}, byte_ratio={byte_ratio:.1%}, project_score={project_score}'
        return result
    result['reason'] = f'Not enough document evidence: document_ratio={ratio:.1%}, byte_ratio={byte_ratio:.1%}'
    return result

def zip_entry_timestamp(info: zipfile.ZipInfo) -> float:
    try:
        return datetime(*info.date_time).timestamp()
    except Exception:
        return datetime.now().timestamp()
