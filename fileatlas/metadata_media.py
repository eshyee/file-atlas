"""Read-only media metadata and classification helpers extracted from v6.

This module never moves, extracts, overwrites, or deletes user files.
MetadataReader invokes optional local ExifTool/FFprobe tools with timeouts.
"""
import json
import os
import re
import shutil
import subprocess
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from PIL import Image, ExifTags
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False
BUFFER_SIZE = 16 * 1024 * 1024
MIN_FREE_SPACE = 500 * 1024 * 1024
CLASSIFY_DIR_NAMES = {'picture_classify', 'video_classify', 'audio_classify'}
SKIP_DIRECTORY_NAMES = {'$recycle.bin', 'system volume information', 'media_classify_logs', 'document_classify_logs', 'all_classify_logs', 'organizer_logs'}
RAW_EXTENSIONS = {'.3fr', '.ari', '.arw', '.bay', '.cr2', '.cr3', '.crw', '.dcr', '.dng', '.erf', '.fff', '.iiq', '.k25', '.kdc', '.mdc', '.mef', '.mos', '.mrw', '.nef', '.nrw', '.orf', '.pef', '.ptx', '.pxn', '.r3d', '.raf', '.raw', '.rwl', '.rw2', '.rwz', '.sr2', '.srf', '.srw', '.x3f'}
PHOTO_LIKE_EXTENSIONS = {'.jpg', '.jpeg', '.jpe', '.jfif', '.heic', '.heif', '.avif', '.tif', '.tiff', '.webp'} | RAW_EXTENSIONS
VECTOR_EXTENSIONS = {'.svg', '.svgz', '.eps', '.wmf', '.emf'}
DESIGN_EXTENSIONS = {'.psd', '.psb', '.xcf', '.kra', '.ora'}
ICON_EXTENSIONS = {'.ico', '.icns', '.cur'}
ANIMATED_EXTENSIONS = {'.gif', '.apng'}
RENDER_EXTENSIONS = {'.exr', '.hdr', '.pic', '.rgbe', '.pfm'}
RASTER_GRAPHIC_EXTENSIONS = {'.png', '.bmp', '.dib', '.pbm', '.pgm', '.ppm', '.pnm', '.tga', '.pcx', '.xbm', '.xpm', '.dds', '.sgi', '.ras', '.jp2', '.j2k', '.jpf', '.jpx', '.jpm', '.mj2', '.qoi'}
MEDIA_EXCLUDED_DOCUMENT_EXTENSIONS = {'.pdf', '.xps', '.oxps'}
PIL_EXTENSIONS = {ext.lower() for ext in Image.registered_extensions().keys() if ext.lower() not in MEDIA_EXCLUDED_DOCUMENT_EXTENSIONS}
ALL_IMAGE_EXTENSIONS = PIL_EXTENSIONS | RAW_EXTENSIONS | PHOTO_LIKE_EXTENSIONS | VECTOR_EXTENSIONS | DESIGN_EXTENSIONS | ICON_EXTENSIONS | ANIMATED_EXTENSIONS | RENDER_EXTENSIONS | RASTER_GRAPHIC_EXTENSIONS
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.mkv', '.avi', '.wmv', '.asf', '.flv', '.f4v', '.webm', '.mpg', '.mpeg', '.mpe', '.m2v', '.mts', '.m2ts', '.ts', '.vob', '.3gp', '.3g2', '.mxf', '.dv', '.ogv', '.rm', '.rmvb', '.mod', '.tod', '.braw', '.r3d'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.wave', '.flac', '.aac', '.m4a', '.m4b', '.m4p', '.alac', '.ogg', '.oga', '.opus', '.wma', '.aiff', '.aif', '.aifc', '.ape', '.wv', '.amr', '.ac3', '.eac3', '.dts', '.mka', '.caf', '.au', '.snd', '.mid', '.midi', '.ra', '.tta', '.dsf', '.dff', '.pcm', '.tak', '.spx'}
PROJECT_EXTENSIONS = {'.py', '.pyc', '.ipynb', '.js', '.ts', '.tsx', '.jsx', '.html', '.htm', '.css', '.scss', '.less', '.c', '.cc', '.cpp', '.h', '.hpp', '.rs', '.go', '.java', '.cs', '.sln', '.vcxproj', '.csproj', '.gradle', '.toml', '.yaml', '.yml', '.ini', '.cfg', '.blend', '.fbx', '.obj', '.mtl', '.gltf', '.glb', '.prproj', '.aep', '.drp', '.unity', '.unitypackage', '.uproject', '.psd', '.psb', '.ai', '.indd', '.kicad_pcb', '.kicad_sch', '.brd', '.sch', '.mph', '.wbpj', '.cae', '.odb'}
PROJECT_FILENAMES = {'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'cargo.toml', 'cargo.lock', 'cmakelists.txt', 'makefile', 'requirements.txt', 'pyproject.toml', 'dockerfile'}
PROJECT_PATH_PARTS = {'.git', 'node_modules', 'src', 'source', 'assets', 'textures', 'sprites', 'www', 'public', 'build', 'dist', 'vendor'}
ZIP_JUNK_NAMES = {'.ds_store', 'thumbs.db', 'desktop.ini'}
CAMERA_IMAGE_FILENAME_RE = re.compile('^(_?DSC(?:_|\\d)|IMG_|DSCF|DJI_|PXL_|MVIMG_|SAM_|GOPR|GH\\d|C\\d{4,})', re.IGNORECASE)
CAMERA_VIDEO_FILENAME_RE = re.compile('^(C\\d{4,}|MVI_|MOV_|VID_|VIDEO_|DJI_|GOPR|GP\\d|GH\\d|GX\\d|PXL_|MVIMG_)', re.IGNORECASE)
AUDIO_RECORDING_FILENAME_RE = re.compile('^(REC|RECORD|VOICE|MEMO|AUDIO|ZOOM|TASCAM|DR-\\d|H\\d_|R\\d{6,})', re.IGNORECASE)
SCREEN_RECORDING_KEYWORDS = {'obs', 'obs studio', 'bandicam', 'xbox game bar', 'game bar', 'screen recorder', 'screenrecord', 'screen recording', 'nvidia share', 'geforce experience', 'shadowplay', 'camtasia', 'snagit', 'quicktime player screen recording'}
VIDEO_EDITING_KEYWORDS = {'adobe premiere', 'premiere pro', 'davinci resolve', 'final cut', 'final cut pro', 'vegas', 'vegas pro', 'handbrake', 'after effects', 'capcut', '剪映'}
AUDIO_EDITING_KEYWORDS = {'adobe audition', 'audacity', 'reaper', 'ableton', 'ableton live', 'fl studio', 'logic pro', 'pro tools', 'cubase', 'studio one', 'garageband', 'reason'}
VOICE_RECORDER_KEYWORDS = {'voice memo', 'voice memos', 'voice recorder', 'audio recorder', 'recorder', 'zoom', 'tascam', 'sony recorder', 'olympus recorder'}
TAG_MAKE = 271
TAG_MODEL = 272
TAG_SOFTWARE = 305
TAG_EXIF_IFD = 34665
TAG_DATETIME_ORIGINAL = 36867
TAG_EXPOSURE_TIME = 33434
TAG_FNUMBER = 33437
TAG_ISO = 34855
TAG_FOCAL_LENGTH = 37386
TAG_LENS_MODEL = 42036

def sanitize_folder_name(text) -> str:
    if not text:
        return ''
    text = str(text).strip()
    text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = re.sub('[^A-Za-z0-9_-]+', '_', text)
    text = re.sub('_+', '_', text).strip('_-')
    return text[:100]

def is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False

def get_creation_timestamp(path: Path) -> float:
    return os.path.getctime(path)

def normalize_meta_key(key) -> str:
    return re.sub('[^a-z0-9]+', '', str(key).lower())

def meta_value(metadata: dict, *candidate_names):
    wanted = {normalize_meta_key(name) for name in candidate_names}
    for key, value in metadata.items():
        if value in (None, '', [], {}):
            continue
        raw_key = str(key)
        full_key = normalize_meta_key(raw_key)
        tail = re.split('[:./\\\\]+', raw_key)[-1]
        tail_key = normalize_meta_key(tail)
        if full_key in wanted or tail_key in wanted:
            return value
    return ''

def metadata_text(metadata: dict) -> str:
    values = []
    for value in metadata.values():
        if isinstance(value, (str, int, float)):
            values.append(str(value))
    return ' '.join(values).lower()

def _valid_media_datetime_parts(year, month, day, hour=0, minute=0, second=0):
    try:
        year = int(year)
        month = int(month)
        day = int(day)
        hour = int(hour or 0)
        minute = int(minute or 0)
        second = int(float(second or 0))
        if not 1970 <= year <= 2100:
            return None
        return datetime(year, month, day, hour, minute, second)
    except Exception:
        return None

def parse_media_datetime_value(value):
    if value in (None, '', [], {}):
        return None
    text = str(value).strip()
    match = re.search('(?<!\\d)(19[7-9]\\d|20\\d{2}|2100)[:/\\-\\.](\\d{1,2})[:/\\-\\.](\\d{1,2})(?:[T\\s]+(\\d{1,2})[:\\.](\\d{1,2})(?:[:\\.](\\d{1,2}(?:\\.\\d+)?))?)?', text)
    if not match:
        return None
    return _valid_media_datetime_parts(match.group(1), match.group(2), match.group(3), match.group(4) or 0, match.group(5) or 0, match.group(6) or 0)

def extract_datetime_from_filename(filename: str):
    stem = Path(filename).stem
    separated = re.search('(?<!\\d)(19[7-9]\\d|20\\d{2}|2100)[-_. ](\\d{1,2})[-_. ](\\d{1,2})(?:[ T_-]+(\\d{1,2})[-_.:](\\d{1,2})(?:[-_.:](\\d{1,2}))?)?', stem)
    if separated:
        dt = _valid_media_datetime_parts(separated.group(1), separated.group(2), separated.group(3), separated.group(4) or 0, separated.group(5) or 0, separated.group(6) or 0)
        if dt is not None:
            return dt
    compact = re.search('(?<!\\d)(19[7-9]\\d|20\\d{2}|2100)(\\d{2})(\\d{2})(?:[_\\-. ]?(\\d{2})(\\d{2})(\\d{2}))?(?!\\d)', stem)
    if compact:
        dt = _valid_media_datetime_parts(compact.group(1), compact.group(2), compact.group(3), compact.group(4) or 0, compact.group(5) or 0, compact.group(6) or 0)
        if dt is not None:
            return dt
    return None

def _metadata_datetime_candidates(media_type: str):
    if media_type == 'picture':
        return ('DateTimeOriginal', 'SubSecDateTimeOriginal', 'CreateDate', 'DateCreated', 'ContentCreateDate')
    if media_type == 'video':
        return ('DateTimeOriginal', 'MediaCreateDate', 'CreateDate', 'TrackCreateDate', 'ContentCreateDate', 'CreationDate')
    if media_type == 'audio':
        return ('DateTimeOriginal', 'MediaCreateDate', 'CreateDate', 'RecordingTime', 'DateCreated', 'CreationDate')
    return ('DateTimeOriginal', 'CreateDate')

def find_metadata_datetime(metadata: dict, media_type: str):
    wanted = {normalize_meta_key(name): name for name in _metadata_datetime_candidates(media_type)}
    found = []
    for key, value in metadata.items():
        raw_key = str(key)
        tail = re.split('[:./\\\\]+', raw_key)[-1]
        normalized = normalize_meta_key(tail)
        if normalized not in wanted:
            continue
        dt = parse_media_datetime_value(value)
        if dt is not None:
            found.append((dt, wanted[normalized]))
    if not found:
        return (None, '')
    found.sort(key=lambda item: item[0])
    return found[0]

def resolve_media_datetime(path: Path, metadata: dict, media_type: str, fallback_timestamp=None, fallback_source='WINDOWS_CREATION_TIME', filename=None):
    candidates = []
    metadata_dt, metadata_tag = find_metadata_datetime(metadata, media_type)
    if metadata_dt is not None:
        candidates.append((metadata_dt, f'METADATA:{metadata_tag}'))
    name_for_date = filename if filename is not None else path.name
    filename_dt = extract_datetime_from_filename(name_for_date)
    if filename_dt is not None:
        candidates.append((filename_dt, 'FILENAME_DATE'))
    if fallback_timestamp is None:
        fallback_timestamp = get_creation_timestamp(path)
    try:
        fallback_dt = datetime.fromtimestamp(fallback_timestamp)
        candidates.append((fallback_dt, fallback_source))
    except Exception:
        pass
    if not candidates:
        return (datetime.now(), 'FALLBACK_NOW')
    candidates.sort(key=lambda item: item[0])
    return candidates[0]

def contains_any(text: str, keywords) -> bool:
    text = text.lower()
    return any((keyword.lower() in text for keyword in keywords))

def find_executable(script_dir: Path, names):
    for name in names:
        local = script_dir / name
        if local.exists():
            return str(local)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None

class MetadataReader:

    def __init__(self, script_dir: Path):
        self.script_dir = script_dir
        self.exiftool = find_executable(script_dir, ['exiftool.exe', 'exiftool'])
        self.exiftool_k = find_executable(script_dir, ['exiftool(-k).exe'])
        self.ffprobe = find_executable(script_dir, ['ffprobe.exe', 'ffprobe'])
        self.helper = None
        self.exiftool_mode = 'Not available'

    def __enter__(self):
        if self.exiftool:
            try:
                completed = subprocess.run([self.exiftool, '-ver'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', timeout=8, check=False)
                version = (completed.stdout or '').strip()
                if completed.returncode == 0 and version:
                    self.exiftool_mode = f'ExifTool CLI safe mode: {self.exiftool} | version {version}'
                else:
                    self.exiftool = None
                    self.exiftool_mode = 'ExifTool self-test failed; using Pillow/FFprobe fallback'
            except subprocess.TimeoutExpired:
                self.exiftool = None
                self.exiftool_mode = 'ExifTool startup timed out; using Pillow/FFprobe fallback'
            except Exception as error:
                self.exiftool = None
                self.exiftool_mode = f'ExifTool unavailable ({error}); using Pillow/FFprobe fallback'
        elif self.exiftool_k:
            self.exiftool_mode = 'Found exiftool(-k).exe but disabled it because (-k) may wait for keyboard input. Rename it to exiftool.exe for RAW metadata support.'
        else:
            self.exiftool_mode = 'ExifTool not found; using Pillow/FFprobe fallback'
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.helper is not None:
            try:
                self.helper.__exit__(exc_type, exc, tb)
            except Exception:
                pass

    def read_exiftool(self, path: Path) -> dict:
        if self.exiftool:
            try:
                command = [self.exiftool, '-j', '-G1', '-n', str(path)]
                completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', timeout=20, check=False)
                rows = json.loads(completed.stdout or '[]')
                if rows:
                    return rows[0]
            except Exception:
                pass
        return {}

    @staticmethod
    def read_pillow(path: Path) -> dict:
        result = {}
        try:
            with Image.open(path) as img:
                result['Pillow:FileType'] = img.format or ''
                result['Pillow:ImageWidth'] = getattr(img, 'width', '')
                result['Pillow:ImageHeight'] = getattr(img, 'height', '')
                exif = img.getexif()
                if not exif:
                    return result
                result['Pillow:Make'] = exif.get(TAG_MAKE, '')
                result['Pillow:Model'] = exif.get(TAG_MODEL, '')
                result['Pillow:Software'] = exif.get(TAG_SOFTWARE, '')
                try:
                    if hasattr(ExifTags, 'IFD'):
                        sub = exif.get_ifd(ExifTags.IFD.Exif)
                    else:
                        sub = exif.get_ifd(TAG_EXIF_IFD)
                    if sub:
                        result['Pillow:DateTimeOriginal'] = sub.get(TAG_DATETIME_ORIGINAL, '')
                        result['Pillow:ExposureTime'] = sub.get(TAG_EXPOSURE_TIME, '')
                        result['Pillow:FNumber'] = sub.get(TAG_FNUMBER, '')
                        result['Pillow:ISO'] = sub.get(TAG_ISO, '')
                        result['Pillow:FocalLength'] = sub.get(TAG_FOCAL_LENGTH, '')
                        result['Pillow:LensModel'] = sub.get(TAG_LENS_MODEL, '')
                except Exception:
                    pass
        except Exception:
            pass
        return result

    def read_ffprobe(self, path: Path) -> dict:
        if not self.ffprobe:
            return {}
        try:
            command = [self.ffprobe, '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(path)]
            completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', timeout=30, check=False)
            data = json.loads(completed.stdout or '{}')
        except Exception:
            return {}
        result = {}
        fmt = data.get('format', {})
        for key, value in fmt.items():
            if key == 'tags':
                continue
            result[f'FFProbe:Format:{key}'] = value
        for key, value in (fmt.get('tags', {}) or {}).items():
            result[f'FFProbe:FormatTag:{key}'] = value
        streams = data.get('streams', [])
        for index, stream in enumerate(streams):
            for key in ('codec_name', 'codec_long_name', 'codec_type', 'profile', 'width', 'height', 'sample_rate', 'channels', 'channel_layout'):
                if key in stream:
                    result[f'FFProbe:Stream{index}:{key}'] = stream[key]
            for key, value in (stream.get('tags', {}) or {}).items():
                result[f'FFProbe:Stream{index}Tag:{key}'] = value
        return result

    def read(self, path: Path, media_type: str) -> dict:
        result = {}
        exiftool_data = self.read_exiftool(path)
        result.update(exiftool_data)
        if media_type == 'picture':
            if not exiftool_data or (not meta_value(exiftool_data, 'Make') and (not meta_value(exiftool_data, 'Model'))):
                result.update(self.read_pillow(path))
        elif media_type in {'video', 'audio'}:
            ff_data = self.read_ffprobe(path)
            result.update(ff_data)
        return result

def quick_pillow_image_check(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            _ = img.format
        return True
    except Exception:
        return False

def media_type_from_name(name: str):
    ext = Path(name).suffix.lower()
    if ext in MEDIA_EXCLUDED_DOCUMENT_EXTENSIONS:
        return None
    # RED R3D contains camera video, even though older RAW sets included it.
    if ext == '.r3d':
        return 'video'
    if ext in ALL_IMAGE_EXTENSIONS:
        return 'picture'
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    return None

def device_folder_from_metadata(metadata: dict):
    make = str(meta_value(metadata, 'Make', 'Manufacturer', 'DeviceManufacturer', 'CameraManufacturer', 'CameraMake', 'com.apple.quicktime.make') or '').strip()
    model = str(meta_value(metadata, 'Model', 'CameraModelName', 'DeviceModelName', 'DeviceModel', 'CameraModel', 'ModelID', 'SonyModelID', 'CameraModelID', 'com.apple.quicktime.model') or '').strip()
    if not model:
        software_or_program = str(meta_value(metadata, 'Software', 'ProgramName', 'CreatorTool', 'FirmwareVersion', 'SoftwareVersion') or '').strip()
        sony_model_match = re.search('\\b(ILCE[-_ ]?[A-Za-z0-9]+|ILME[-_ ]?[A-Za-z0-9]+|NEX[-_ ]?[A-Za-z0-9]+|DSC[-_ ]?[A-Za-z0-9]+|ZV[-_ ]?[A-Za-z0-9]+|SLT[-_ ]?[A-Za-z0-9]+|ILCA[-_ ]?[A-Za-z0-9]+)\\b', software_or_program, re.IGNORECASE)
        if sony_model_match:
            model = sony_model_match.group(1).replace('_', '-').replace(' ', '-').upper()
    make_clean = sanitize_folder_name(make)
    model_clean = sanitize_folder_name(model)
    if make_clean.lower() in {'sony', 'sonycorporation', 'sony_corporation'}:
        make_clean = 'SONY'
    if make_clean and model_clean:
        if model_clean.lower().startswith(make_clean.lower()):
            device = model_clean
        else:
            device = f'{make_clean}_{model_clean}'
    elif model_clean:
        device = model_clean
    elif make_clean:
        device = make_clean
    else:
        device = ''
    return (device, make, model)

def image_noncamera_subtype(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VECTOR_EXTENSIONS:
        return 'Vector'
    if ext in DESIGN_EXTENSIONS:
        return 'Design'
    if ext in ICON_EXTENSIONS:
        return 'Icon'
    if ext in ANIMATED_EXTENSIONS:
        return 'Animated'
    if ext in RENDER_EXTENSIONS:
        return 'Render_HDR'
    if ext in {'.gif', '.webp', '.png'}:
        try:
            with Image.open(path) as img:
                if getattr(img, 'is_animated', False):
                    return 'Animated'
        except Exception:
            pass
    if ext in RASTER_GRAPHIC_EXTENSIONS or ext in {'.png', '.bmp', '.dib'}:
        return 'Raster'
    return 'OtherImage'

def classify_picture(path: Path, metadata: dict) -> dict:
    ext = path.suffix.lower()
    device, make, model = device_folder_from_metadata(metadata)
    software = str(meta_value(metadata, 'Software', 'CreatorTool', 'ProcessingSoftware') or '').strip()
    lens = str(meta_value(metadata, 'LensModel', 'Lens') or '').strip()
    if device:
        return {'kind': 'Camera', 'root': device, 'subtype': '', 'make': make, 'model': model, 'software': software, 'reason': 'Camera Make/Model metadata'}
    if ext in RAW_EXTENSIONS:
        return {'kind': 'Camera', 'root': 'UnknownCamera', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'RAW image without readable camera model'}
    if CAMERA_IMAGE_FILENAME_RE.search(path.name):
        return {'kind': 'Camera', 'root': 'UnknownCamera', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Camera-like image filename'}
    shooting_fields = [meta_value(metadata, 'DateTimeOriginal'), meta_value(metadata, 'ExposureTime'), meta_value(metadata, 'FNumber'), meta_value(metadata, 'ISO'), meta_value(metadata, 'FocalLength'), lens]
    shooting_count = sum((value not in (None, '') for value in shooting_fields))
    if shooting_count >= 2:
        return {'kind': 'Camera', 'root': 'UnknownCamera', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': f'{shooting_count} shooting metadata fields found'}
    if ext in VECTOR_EXTENSIONS or ext in DESIGN_EXTENSIONS or ext in ICON_EXTENSIONS or (ext in ANIMATED_EXTENSIONS) or (ext in RENDER_EXTENSIONS) or (ext in RASTER_GRAPHIC_EXTENSIONS) or (ext in {'.png', '.bmp', '.dib', '.gif'}):
        subtype = image_noncamera_subtype(path)
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': subtype, 'make': '', 'model': '', 'software': software, 'reason': f'Non-camera image type: {subtype}'}
    if ext in PHOTO_LIKE_EXTENSIONS:
        return {'kind': 'UnknownPhoto', 'root': 'UnknownPhoto', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Photo-like image without camera metadata'}
    return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'OtherImage', 'make': '', 'model': '', 'software': software, 'reason': 'Other recognized image format'}

def classify_video(path: Path, metadata: dict) -> dict:
    device, make, model = device_folder_from_metadata(metadata)
    if device:
        return {'kind': 'Camera', 'root': device, 'subtype': '', 'make': make, 'model': model, 'software': str(meta_value(metadata, 'Software', 'Encoder', 'EncodedBy', 'WritingApplication') or ''), 'reason': 'Video device Make/Model metadata'}
    text = metadata_text(metadata)
    software = str(meta_value(metadata, 'Software', 'Encoder', 'EncodedBy', 'WritingApplication', 'HandlerDescription', 'CompressorName') or '').strip()
    if re.search('^(SVID_|SCREENREC(?:ORD)?_|SCREEN_RECORD(?:ING)?_|SCREENRECORD(?:ING)?_)', path.name, re.IGNORECASE):
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'ScreenRecording', 'make': '', 'model': '', 'software': software, 'reason': 'Screen-recording filename pattern'}
    if contains_any(text, SCREEN_RECORDING_KEYWORDS):
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'ScreenRecording', 'make': '', 'model': '', 'software': software, 'reason': 'Screen-recording software metadata'}
    if contains_any(text, VIDEO_EDITING_KEYWORDS):
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'EditedVideo', 'make': '', 'model': '', 'software': software, 'reason': 'Video editing/export software metadata'}
    if CAMERA_VIDEO_FILENAME_RE.search(path.name):
        return {'kind': 'Camera', 'root': 'UnknownCamera', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Camera-like video filename'}
    if any((meta_value(metadata, key) not in (None, '') for key in ('GPSCoordinates', 'GPSLatitude', 'GPSLongitude', 'CameraModelName', 'DeviceModelName'))):
        return {'kind': 'Camera', 'root': 'UnknownCamera', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Camera/mobile metadata without readable model'}
    return {'kind': 'UnknownSource', 'root': 'UnknownSource', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Video source cannot be determined reliably'}

def classify_audio(path: Path, metadata: dict) -> dict:
    device, make, model = device_folder_from_metadata(metadata)
    software = str(meta_value(metadata, 'Software', 'Encoder', 'EncodedBy', 'WritingApplication') or '').strip()
    if device:
        return {'kind': 'RecordingDevice', 'root': device, 'subtype': '', 'make': make, 'model': model, 'software': software, 'reason': 'Audio recording device Make/Model metadata'}
    text = metadata_text(metadata)
    if contains_any(text, AUDIO_EDITING_KEYWORDS):
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'EditedAudio', 'make': '', 'model': '', 'software': software, 'reason': 'Audio editing/DAW metadata'}
    if contains_any(text, VOICE_RECORDER_KEYWORDS) or AUDIO_RECORDING_FILENAME_RE.search(path.name):
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'VoiceRecording', 'make': '', 'model': '', 'software': software, 'reason': 'Voice recorder metadata or recording-like filename'}
    artist = meta_value(metadata, 'Artist', 'AlbumArtist')
    album = meta_value(metadata, 'Album')
    track = meta_value(metadata, 'Track', 'TrackNumber')
    genre = meta_value(metadata, 'Genre')
    title = meta_value(metadata, 'Title')
    music_tag_count = sum((value not in (None, '') for value in (artist, album, track, genre)))
    if music_tag_count >= 2:
        return {'kind': 'NonCamera', 'root': 'NonCamera', 'subtype': 'Music', 'make': '', 'model': '', 'software': software, 'reason': 'Music metadata tags'}
    if path.suffix.lower() in {'.wav', '.wave', '.aiff', '.aif', '.aifc', '.caf', '.pcm'}:
        return {'kind': 'UnknownRecording', 'root': 'UnknownRecording', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Recording-oriented audio format without device metadata'}
    if title:
        return {'kind': 'UnknownAudio', 'root': 'UnknownAudio', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Tagged audio, but type cannot be determined reliably'}
    return {'kind': 'UnknownAudio', 'root': 'UnknownAudio', 'subtype': '', 'make': '', 'model': '', 'software': software, 'reason': 'Audio source/type cannot be determined reliably'}

def classify_media(media_type: str, path: Path, metadata: dict) -> dict:
    if media_type == 'picture':
        return classify_picture(path, metadata)
    if media_type == 'video':
        return classify_video(path, metadata)
    if media_type == 'audio':
        return classify_audio(path, metadata)
    raise ValueError(f'Unknown media type: {media_type}')

def classification_root(source_root: Path, media_type: str) -> Path:
    if media_type == 'picture':
        return source_root / 'picture_classify'
    if media_type == 'video':
        return source_root / 'video_classify'
    if media_type == 'audio':
        return source_root / 'audio_classify'
    raise ValueError(media_type)

def build_destination(source_root: Path, media_type: str, classification: dict, month: str, filename: str) -> Path:
    month_match = re.fullmatch('(\\d{4})-(\\d{2})', str(month))
    year = month_match.group(1) if month_match else 'UnknownYear'
    root = classification_root(source_root, media_type) / year / month / classification['root']
    if classification['root'] == 'NonCamera' and classification['subtype']:
        root = root / classification['subtype']
    return root / filename

def zip_member_is_junk(info: zipfile.ZipInfo) -> bool:
    name = info.filename.replace('\\', '/')
    parts = [part for part in name.split('/') if part]
    if not parts:
        return True
    if '__MACOSX' in parts:
        return True
    if parts[-1].lower() in ZIP_JUNK_NAMES:
        return True
    return False

def project_indicator_score(name: str) -> int:
    normalized = name.replace('\\', '/')
    parts = [part.lower() for part in normalized.split('/') if part]
    basename = parts[-1] if parts else ''
    ext = Path(basename).suffix.lower()
    score = 0
    if ext in PROJECT_EXTENSIONS:
        score += 2
    if basename in PROJECT_FILENAMES:
        score += 3
    if any((part in PROJECT_PATH_PARTS for part in parts[:-1])):
        score += 1
    return score

def analyze_zip(zip_path: Path, enabled_types, min_media_count: int, min_media_ratio: float, min_byte_ratio: float):
    result = {'status': 'NOT_MEDIA_ARCHIVE', 'reason': '', 'total_files': 0, 'media_count': 0, 'media_ratio': 0.0, 'media_bytes_ratio': 0.0, 'picture_count': 0, 'video_count': 0, 'audio_count': 0, 'project_score': 0, 'pure_media': False, 'encrypted': False, 'uncompressed_total': 0, 'uncompressed_media': 0, 'media_infos': []}
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            infos = [info for info in zf.infolist() if not info.is_dir() and (not zip_member_is_junk(info))]
    except Exception as error:
        result['status'] = 'ZIP_ERROR'
        result['reason'] = str(error)
        return result
    if not infos:
        result['reason'] = 'No meaningful files'
        return result
    total_bytes = 0
    media_bytes = 0
    media_infos = []
    counts = {'picture': 0, 'video': 0, 'audio': 0}
    project_score = 0
    encrypted = False
    for info in infos:
        total_bytes += max(0, info.file_size)
        if info.flag_bits & 1:
            encrypted = True
        project_score += project_indicator_score(info.filename)
        media_type = media_type_from_name(info.filename)
        if media_type and media_type in enabled_types:
            counts[media_type] += 1
            media_infos.append((info, media_type))
            media_bytes += max(0, info.file_size)
    total_files = len(infos)
    media_count = len(media_infos)
    media_ratio = media_count / total_files if total_files else 0.0
    media_bytes_ratio = media_bytes / total_bytes if total_bytes else 0.0
    pure_media = media_count == total_files and total_files > 0
    result.update({'total_files': total_files, 'media_count': media_count, 'media_ratio': media_ratio, 'media_bytes_ratio': media_bytes_ratio, 'picture_count': counts['picture'], 'video_count': counts['video'], 'audio_count': counts['audio'], 'project_score': project_score, 'pure_media': pure_media, 'encrypted': encrypted, 'uncompressed_total': total_bytes, 'uncompressed_media': media_bytes, 'media_infos': media_infos})
    if encrypted:
        result['status'] = 'ENCRYPTED'
        result['reason'] = 'Archive contains encrypted members'
        return result
    if media_count == 0:
        result['reason'] = 'No enabled media files'
        return result
    small_pure_media = pure_media and media_count <= 4
    enough_count = media_count >= min_media_count
    ratio_ok = media_ratio >= min_media_ratio
    bytes_ok = media_bytes_ratio >= min_byte_ratio
    project_penalty = project_score > 0 and (not pure_media)
    if small_pure_media:
        result['status'] = 'MEDIA_ARCHIVE'
        result['reason'] = 'Small pure media archive'
        return result
    if enough_count and ratio_ok and bytes_ok and (not project_penalty):
        result['status'] = 'MEDIA_ARCHIVE'
        result['reason'] = f'media_ratio={media_ratio:.1%}, byte_ratio={media_bytes_ratio:.1%}'
        return result
    if media_ratio >= 0.35 or media_count >= 10:
        result['status'] = 'REVIEW'
        result['reason'] = f'Borderline archive: media_ratio={media_ratio:.1%}, byte_ratio={media_bytes_ratio:.1%}, project_score={project_score}'
        return result
    result['reason'] = f'Not enough media evidence: media_ratio={media_ratio:.1%}, byte_ratio={media_bytes_ratio:.1%}'
    return result

def zip_entry_timestamp(info: zipfile.ZipInfo) -> float:
    try:
        dt = datetime(*info.date_time)
        return dt.timestamp()
    except Exception:
        return datetime.now().timestamp()
