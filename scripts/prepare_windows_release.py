"""Validate a built EXE and create the public ZIP with its runtime notices.

Only a fixed staging directory beneath workspace/build may be recursively
removed. Release artifacts are replaced individually after a complete ZIP is
ready. User files, scan state, and unrelated workspace directories are ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import stat
import sys
import tomllib
import zipfile
from pathlib import Path


MODELS = {
    'face_detection_yunet_2023mar.onnx': '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4',
    'face_recognition_sface_2021dec.onnx': '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79',
    'YuNet-LICENSE.txt': 'c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958',
    'SFace-LICENSE.txt': 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30',
}
FRONTEND_PACKAGES = ('react', 'react-dom', 'scheduler', 'lucide-react')
LICENSE_PREFIXES = ('license', 'copying', 'notice', 'copyright', 'authors')


def generated_path(workspace: Path, area: str, child: str | None = None):
    """Reject links/junctions and any computed target outside build/release."""
    workspace = workspace.resolve(strict=True)
    if area not in ('build', 'release') or child not in (None, 'windows-release'):
        raise ValueError('Only fixed workspace build/release outputs are allowed.')
    target = workspace / area
    if child:
        target /= child
    for component in [workspace / area, target]:
        if os.path.lexists(component):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
                raise ValueError(f'Generated output cannot be a link or reparse point: {component}')
    resolved = target.resolve()
    if not resolved.is_relative_to(workspace) or resolved == workspace or resolved != target:
        raise ValueError('Generated output escaped the workspace.')
    return resolved


def _copy(source: Path, destination: Path):
    if not source.is_file():
        raise FileNotFoundError(f'Required release input is missing: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def inspect_executable(executable: Path, include_models: bool):
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    names = {name.replace('\\', '/'): name for name in archive.toc}
    if any('pillow_heif' in name.lower() for name in names):
        raise ValueError('The official Windows EXE must not bundle pillow-heif.')
    modules = {package for package in ('cv2', 'numpy') if any(name.startswith(package + '/') for name in names)}
    if include_models:
        if modules != {'cv2', 'numpy'}:
            raise ValueError('The full Windows EXE is missing OpenCV or NumPy runtime files.')
        for filename, expected in MODELS.items():
            entry = names.get('models/' + filename)
            if entry is None or hashlib.sha256(archive.extract(entry)).hexdigest() != expected:
                raise ValueError(f'Bundled model/license is missing or has a wrong checksum: {filename}')
    return modules


def _package_licenses(distribution: str, destination: Path):
    installed = metadata.distribution(distribution)
    count = 0
    for relative in installed.files or []:
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            continue
        if not relative.name.lower().startswith(LICENSE_PREFIXES):
            continue
        # Exclude data whose filename merely contains a license concept.
        if relative.suffix.lower() in ('.py', '.pyc', '.pyd', '.dll', '.xml'):
            continue
        _copy(Path(installed.locate_file(relative)), destination / relative)
        count += 1
    if not count:
        raise ValueError(f'No installed runtime license text was found for {distribution}.')
    return {'name': distribution, 'version': installed.version, 'license_files': count}


def collect_licenses(workspace: Path, stage: Path, modules, include_models: bool):
    licenses = stage / 'licenses'
    _copy(Path(sys.base_prefix) / 'LICENSE.txt', licenses / 'Python' / 'LICENSE.txt')
    _copy(workspace / 'scripts' / 'licenses' / 'Tcl8.6-LICENSE.txt', licenses / 'Tcl' / 'LICENSE.txt')
    tcl_root = Path(sys.base_prefix) / 'tcl'
    tk_licenses = sorted(tcl_root.glob('tk*/license.terms'))
    if not tk_licenses:
        raise ValueError('The Python installation is missing the Tk runtime license.')
    _copy(tk_licenses[-1], licenses / 'Tk' / 'license.terms')
    packages = ['Pillow', 'pyinstaller']
    packages += ['numpy'] if 'numpy' in modules else []
    packages += ['opencv-python-headless'] if 'cv2' in modules else []
    records = [_package_licenses(name, licenses / name) for name in packages]
    for name in FRONTEND_PACKAGES:
        package = workspace / 'frontend' / 'node_modules' / name
        manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
        texts = [path for path in package.iterdir() if path.is_file() and path.name.lower().startswith(LICENSE_PREFIXES)]
        if not texts:
            raise ValueError(f'The frontend runtime license is missing: {name}')
        for text in texts:
            _copy(text, licenses / 'frontend' / name / text.name)
        records.append({'name': name, 'version': manifest['version'], 'license_files': len(texts)})
    if include_models:
        for filename in ('YuNet-LICENSE.txt', 'SFace-LICENSE.txt'):
            _copy(workspace / 'build' / 'face-models' / filename, licenses / 'models' / filename)
    version = tomllib.loads((workspace / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    manifest = {'fileatlas': version, 'python': sys.version.split()[0], 'runtime_packages': records,
                'bundled_models': {name: digest for name, digest in MODELS.items() if name.endswith('.onnx')} if include_models else {},
                'excluded_optional_runtime': ['pillow-heif']}
    (licenses / 'runtime-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def prepare_release(workspace: Path, *, include_models=True):
    workspace = workspace.resolve(strict=True)
    stage = generated_path(workspace, 'build', 'windows-release')
    release = generated_path(workspace, 'release')
    executable = workspace / 'dist' / 'FileAtlas.exe'
    if not executable.is_file() or not executable.resolve().is_relative_to(workspace):
        raise ValueError('Expected a built workspace/dist/FileAtlas.exe.')
    modules = inspect_executable(executable, include_models)
    # All recursive deletion targets are checked immediately before deletion.
    if stage.exists():
        shutil.rmtree(generated_path(workspace, 'build', 'windows-release'))
    stage.mkdir(parents=True)
    _copy(executable, stage / 'FileAtlas.exe')
    for name in ('启动归序.cmd', '以管理员身份启动归序.cmd', 'LICENSE', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md'):
        _copy(workspace / name, stage / name)
    _copy(workspace / 'readme.md', stage / 'README.md')
    for name in ('screenshot.png', 'legacy-integration.md'):
        _copy(workspace / 'docs' / name, stage / 'docs' / name)
    collect_licenses(workspace, stage, modules, include_models)
    release.mkdir(exist_ok=True)
    archive_path = release / 'FileAtlas-Windows.zip'
    partial = release / 'FileAtlas-Windows.zip.partial'
    checksum = release / 'SHA256SUMS.txt'
    checksum_partial = release / 'SHA256SUMS.txt.partial'
    for target in (archive_path, partial, checksum, checksum_partial):
        if os.path.lexists(target):
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024 or info.st_nlink > 1:
                raise ValueError(f'Release artifact must be a plain, unlinked regular file: {target.name}')
    try:
        with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    output.write(path, path.relative_to(stage).as_posix())
        with zipfile.ZipFile(partial) as verified:
            if verified.testzip():
                raise ValueError('ZIP integrity validation failed.')
        with partial.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        checksum_partial.write_text(f'{digest}  {archive_path.name}\n', encoding='ascii')
        os.replace(partial, archive_path)
        os.replace(checksum_partial, checksum)
    finally:
        if partial.exists():
            partial.unlink()
        if checksum_partial.exists():
            checksum_partial.unlink()
    print(json.dumps({'archive': str(archive_path), 'bytes': archive_path.stat().st_size,
                      'sha256': digest, 'face_models': include_models}, ensure_ascii=False))
    return archive_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--without-face-models', action='store_true')
    options = parser.parse_args()
    prepare_release(Path(__file__).resolve().parents[1], include_models=not options.without_face_models)
