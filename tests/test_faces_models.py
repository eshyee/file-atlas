"""Optional real ONNX regression: no model downloads or personal photographs."""

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from fileatlas.faces import (DETECTOR_MODEL, RECOGNIZER_MODEL, FaceIndex,
                             OpenCVBackend, SafetyError)


def test_unicode_model_paths_load_and_run_both_networks():
    cv2 = pytest.importorskip('cv2')
    numpy = pytest.importorskip('numpy')
    if not all(hasattr(cv2, name) and 'bufferModel' in (getattr(cv2, name).create.__doc__ or '')
               for name in ('FaceDetectorYN', 'FaceRecognizerSF')):
        pytest.skip('Optional OpenCV 4.11+ YuNet/SFace buffer APIs are unavailable')
    directory = Path(os.environ.get('FILEATLAS_TEST_MODEL_DIR',
                                   Path(__file__).resolve().parents[1] / 'build' / 'face-models'))
    if not all((directory / name).is_file() for name in (DETECTOR_MODEL, RECOGNIZER_MODEL)):
        pytest.skip('Optional local ONNX weights are absent; tests never download them')
    parent = None
    if os.name == 'nt':
        parent = Path.home() / '.fileatlas-tests'
        parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='real-model-test-', dir=parent) as folder:
        base = Path(folder)
        models = base / '中文 模型'
        models.mkdir()
        for name in (DETECTOR_MODEL, RECOGNIZER_MODEL):
            shutil.copyfile(directory / name, models / name)
        photos = base / '照片'
        photos.mkdir()
        original = photos / '合成图.png'
        Image.new('RGB', (320, 320), (128, 128, 128)).save(original)
        before = original.read_bytes(), original.stat().st_mtime_ns
        loaded = {}

        def backend_factory(detector, recognizer, config):
            loaded['config'] = config
            loaded['backend'] = OpenCVBackend(detector, recognizer, config)
            return loaded['backend']

        index = FaceIndex(base / 'state', backend_factory=backend_factory)
        result = index.build({'sources': [str(photos)], 'model_dir': str(models)})
        assert (result['scanned'], result['indexed'], result['errors']) == (1, 1, 0)
        assert before == (original.read_bytes(), original.stat().st_mtime_ns)
        # Detection has run through build; run the recognizer too even though a
        # uniform synthetic picture deliberately contains no detectable faces.
        features = loaded['backend'].recognizer.feature(numpy.full((112, 112, 3), 128, dtype=numpy.uint8))
        assert features.size == 128
        assert numpy.isfinite(features).all()
        assert numpy.linalg.norm(features) > 0

        # Replacing bytes while preserving size/mtime must fail before OpenCV
        # can load a model different from the verified inference fingerprint.
        model = models / DETECTOR_MODEL
        expected = next(item for item in loaded['config']['models'] if Path(item['path']).name == DETECTOR_MODEL)
        info = model.stat()
        data = bytearray(model.read_bytes())
        data[-1] ^= 1
        model.write_bytes(data)
        os.utime(model, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert hashlib.sha256(data).hexdigest() != expected['sha256']
        with pytest.raises(SafetyError, match='指纹'):
            OpenCVBackend._model_bytes(model, expected)
